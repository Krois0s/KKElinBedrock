import json
import os
import time
import re
import boto3
from botocore.config import Config

s3 = boto3.client('s3')

# 1. 起動時に環境変数からS3設定を取得
BUCKET_NAME = os.environ.get('PROMPT_BUCKET_NAME', 'elin-ai-roleplay-prompts-893061519316-us-east-1-an')

_cached_system_prompt_base = None
_cached_npc_prompts = {}
_prompts_loaded = False

# S3からプロンプト構成ファイルを一括ロード・スキャンする関数
def load_all_prompts():
    global _cached_system_prompt_base, _cached_npc_prompts, _prompts_loaded
    
    try:
        print("S3からプロンプト構成ファイルをスキャン＆ロード中...")
        
        # 1. Header of Load
        try:
            header_res = s3.get_object(Bucket=BUCKET_NAME, Key="system/prompt_header.txt")
            header_text = header_res['Body'].read().decode('utf-8')
        except Exception as e:
            print(f"Headerロード失敗: {e}")
            header_text = "You are the Elin Director System..."
            
        # 2. Footer of Load
        try:
            footer_res = s3.get_object(Bucket=BUCKET_NAME, Key="system/prompt_footer.txt")
            footer_text = footer_res['Body'].read().decode('utf-8')
        except Exception as e:
            print(f"Footerロード失敗: {e}")
            footer_text = "Analyze the given context data..."
            
        # 3. Knowledgeフォルダ以下のスキャンとロード
        knowledge_texts = []
        try:
            list_res = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="knowledge/")
            if 'Contents' in list_res:
                for obj in list_res['Contents']:
                    key = obj['Key']
                    if key.endswith(".txt") or key.endswith(".md"):
                        # ファイル名（拡張子除く）をカテゴリ名として抽出
                        kb_name = key.split("/")[-1].rsplit(".", 1)[0]
                        print(f"Knowledgeファイルロード中: {key} (Name: {kb_name})")
                        file_res = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                        file_text = file_res['Body'].read().decode('utf-8')
                        # 各知識データの識別を容易にするためヘッダーを付与して結合
                        knowledge_texts.append(f"### Knowledge: {kb_name}\n{file_text}")
        except Exception as e:
            print(f"Knowledgeスキャン失敗: {e}")
            
        knowledge_combined = "\n\n".join(knowledge_texts)
        
        # 4. Charactersフォルダ以下のスキャンとロード
        npc_prompts = {}
        uid_to_newest = {}
        try:
            list_res = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="characters/")
            if 'Contents' in list_res:
                # 1. 全ファイルを走査し、UIDごとに最新のファイルを特定
                for obj in list_res['Contents']:
                    key = obj['Key']
                    if key.endswith(".txt"):
                        filename = key.split("/")[-1].replace(".txt", "")
                        # UIDの抽出 (例: 1011_ロベッタ -> 1011)
                        if "_" in filename:
                            uid = filename.split("_", 1)[0]
                        else:
                            uid = filename # 互換用フォールバック
                            
                        last_modified = obj['LastModified']
                        
                        if uid not in uid_to_newest or last_modified > uid_to_newest[uid]['last_modified']:
                            uid_to_newest[uid] = {
                                'key': key,
                                'filename': filename,
                                'last_modified': last_modified
                            }
                
                # 2. 最新のファイルのみをロード
                for uid, info in uid_to_newest.items():
                    filename = info['filename']
                    print(f"NPCプロファイルロード中: {filename}")
                    file_res = s3.get_object(Bucket=BUCKET_NAME, Key=info['key'])
                    file_text = file_res['Body'].read().decode('utf-8')
                    npc_prompts[filename] = file_text
        except Exception as e:
            print(f"Charactersスキャン失敗: {e}")
            
        _cached_system_prompt_base = {
            "header": header_text,
            "knowledge": knowledge_combined,
            "footer": footer_text
        }
        _cached_npc_prompts = npc_prompts
        _prompts_loaded = True
        print(f"S3ロード完了！ 登録NPC数: {len(npc_prompts)}")
        
    except Exception as e:
        print(f"プロンプト一括ロード全体で致命的エラー: {e}")
        _prompts_loaded = False


# 文字列から対応する閉じ中括弧までのJSONブロックを正確に抽出する関数
def extract_json_block(text, start_pos):
    brace_count = 0
    in_string = False
    escape = False
    
    for i in range(start_pos, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    return text[start_pos:i+1]
    return None


# ユーザーメッセージからNPCのプロファイル設定を同期し、リクエストから消去する関数
def process_user_content_and_sync_npcs(content_text):
    global _cached_npc_prompts, _prompts_loaded
    
    s3_updated = False
    json_rewritten = False

    # --- 1. プレイヤーデータ (player_data) の処理 (固定ファイル名版) ---
    idx_player = content_text.find('[player_data]')
    if idx_player != -1:
        start_brace_player = content_text.find('{', idx_player)
        if start_brace_player != -1:
            player_json_str = extract_json_block(content_text, start_brace_player)
            if player_json_str:
                try:
                    player_data = json.loads(player_json_str)
                    player_persona = player_data.get("persona")
                    if player_persona:
                        # 既存キャッシュと比較し、新規/更新があればS3保存 (ファイル名は固定値 '0_player')
                        if "0_player" not in _cached_npc_prompts or _cached_npc_prompts["0_player"] != player_persona:
                            print("新規/更新Playerプロファイル検知")
                            try:
                                s3.put_object(
                                    Bucket=BUCKET_NAME,
                                    Key="characters/0_player.txt",
                                    Body=player_persona.encode('utf-8')
                                )
                                _cached_npc_prompts["0_player"] = player_persona
                                s3_updated = True
                            except Exception as e:
                                print(f"S3へのPlayerプロファイル書き込み失敗: {e}")
                                
                        # リクエストからペルソナを消去してトークンを節約
                        del player_data["persona"]
                        new_player_json_str = json.dumps(player_data, ensure_ascii=False)
                        content_text = content_text.replace(player_json_str, new_player_json_str)
                        json_rewritten = True
                except Exception as e:
                    print(f"player_dataのパースエラー: {e}")

    # --- 2. 近くのキャラクター (nearby_characters) の処理 ---
    idx = content_text.find('[nearby_characters]')
    if idx == -1:
        if s3_updated:
            _prompts_loaded = False
        return content_text
        
    # その後の最初の括弧 '{' の位置を探す
    start_brace = content_text.find('{', idx)
    if start_brace == -1:
        if s3_updated:
            _prompts_loaded = False
        return content_text
        
    # 対応する閉じ中括弧までのJSONブロックを抽出
    json_str = extract_json_block(content_text, start_brace)
    if not json_str:
        if s3_updated:
            _prompts_loaded = False
        return content_text
        
    try:
        char_data = json.loads(json_str)
        characters = char_data.get("characters", {})
        
        for char_name, char_info in characters.items():
            persona = char_info.get("persona")
            uid = char_info.get("uid")
            
            if persona and uid is not None:
                uid_str = str(uid)
                new_filename = f"{uid_str}_{char_name}"
                
                # 既存キャッシュ内から同一UIDのエントリ（改名前の名前など）を探す
                existing_filename = None
                for cached_key in _cached_npc_prompts.keys():
                    if cached_key.startswith(uid_str + "_") or cached_key == uid_str:
                        existing_filename = cached_key
                        break
                
                # S3に未登録、もしくは設定内容が異なる、もしくは名前が変更された場合
                if not existing_filename or _cached_npc_prompts[existing_filename] != persona or existing_filename != new_filename:
                    print(f"新規/更新NPCプロファイル検知: {new_filename}")
                    try:
                        # 新しい名前のファイルを保存
                        s3.put_object(
                            Bucket=BUCKET_NAME,
                            Key=f"characters/{new_filename}.txt",
                            Body=persona.encode('utf-8')
                        )
                        
                        # 改名された場合、古いファイルをS3から削除
                        if existing_filename and existing_filename != new_filename:
                            print(f"NPCの改名を検知。古いファイルを削除します: characters/{existing_filename}.txt")
                            try:
                                s3.delete_object(
                                    Bucket=BUCKET_NAME,
                                    Key=f"characters/{existing_filename}.txt"
                                )
                            except Exception as del_e:
                                print(f"古いファイルの削除失敗: {del_e}")
                            # メモリキャッシュからも古いキーを削除
                            del _cached_npc_prompts[existing_filename]
                        
                        # メモリキャッシュに最新の情報をセット
                        _cached_npc_prompts[new_filename] = persona
                        s3_updated = True
                    except Exception as e:
                        print(f"S3へのNPCプロファイル書き込み失敗 ({new_filename}): {e}")
                
                # リクエストからペルソナを消去してトークンを節約
                del char_info["persona"]
                json_rewritten = True
                
        if json_rewritten:
            new_json_str = json.dumps(char_data, ensure_ascii=False)
            content_text = content_text.replace(json_str, new_json_str)
            
        if s3_updated:
            # 実際にS3上のファイルが追加・更新された場合のみ、プロンプトを再ロードする
            _prompts_loaded = False
            
    except Exception as e:
        print(f"process_user_content_and_sync_npcs でパースエラー: {e}")
        
    return content_text

# 2. 認証キーとBedrockクライアントの設定
MY_API_KEY = os.environ.get("MY_API_KEY", "default-key")

bedrock_client = boto3.client(
    "bedrock-runtime", 
    region_name="us-east-1",
    config=Config(read_timeout=60)
)

SUPPORTED_GROUNDING_MODELS = [
    "amazon.nova-premier-v1:0",
    "us.amazon.nova-premier-v1:0",
    "amazon.nova-pro-v1:0",
    "us.amazon.nova-pro-v1:0",
    "amazon.nova-2-lite-v1:0",
    "us.amazon.nova-2-lite-v1:0"
]

SUPPORTED_1H_TTL_MODELS = [
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0"
]


# 共通定数 (CORSヘッダー)
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
}


# 1. 認証とリクエストパースを行う関数
def parse_and_validate_request(event):
    # CORSプリフライトへの応答
    if event.get("httpMethod") == "OPTIONS":
        return None, {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    # 認証チェック
    auth_header = event.get("headers", {}).get("Authorization", "") or event.get("headers", {}).get("authorization", "")
    if not auth_header or auth_header != f"Bearer {MY_API_KEY}":
        return None, {
            "statusCode": 401,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Unauthorized: Invalid API Key"})
        }

    # リクエストボディの解析
    try:
        body = json.loads(event.get("body", "{}"))
        return body, None
    except Exception:
        return None, {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Invalid JSON"})
        }


# 2. 対象モデルIDとWeb検索（Grounding）の有無を判定する関数
def resolve_model_and_grounding(requested_model):
    enable_grounding = False
    if requested_model.endswith("-search"):
        target_model = requested_model.replace("-search", "")
        if target_model in SUPPORTED_GROUNDING_MODELS:
            enable_grounding = True
        else:
            return None, False, {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": f"Model {target_model} does not support Web Grounding (Search)."})
            }
    else:
        target_model = requested_model
    return target_model, enable_grounding, None


# 3. 送信メッセージを構築する関数 (NPCプロファイル同期・ペルソナ消去も行う)
def prepare_bedrock_messages(openai_messages):
    bedrock_messages = []
    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content")
        
        if role == "system":
            # MODから送られてきたシステムプロンプトは破棄する（S3側の設定を優先するため）
            pass
        elif role == "user":
            # [nearby_characters] や [player_data] をパースして同期＆persona消去
            content = process_user_content_and_sync_npcs(content)
            bedrock_messages.append({
                "role": role,
                "content": [{"text": content}]
            })
        else:
            bedrock_messages.append({
                "role": role,
                "content": [{"text": content}]
            })
    return bedrock_messages


# 4. システムプロンプトを最新状態で構築する関数 (キャッシュTTL設定も行う)
def get_compiled_system_prompt(target_model):
    global _prompts_loaded, _cached_system_prompt_base, _cached_npc_prompts
    
    # 必要に応じてS3からプロンプトをロード
    if not _prompts_loaded:
        load_all_prompts()
        
    # キャッシュからプロンプトを組み立て
    header = _cached_system_prompt_base.get("header", "")
    knowledge = _cached_system_prompt_base.get("knowledge", "")
    footer = _cached_system_prompt_base.get("footer", "")
    
    # 登録されている全NPC（プレイヤー含む）プロファイルを結合
    npc_profiles_list = []
    for filename, npc_persona in _cached_npc_prompts.items():
        if filename == "0_player":
            display_name = "0(Player)"
        elif "_" in filename:
            uid, name = filename.split("_", 1)
            display_name = f"{uid}({name})"
        else:
            display_name = filename
        npc_profiles_list.append(f"### Character Profile: {display_name}\n{npc_persona}")
    npc_profiles_combined = "\n\n".join(npc_profiles_list)
    
    final_system_prompt_text = f"{header}\n\n{knowledge}\n\n## Character Profiles\n{npc_profiles_combined}\n\n{footer}"

    # キャッシュポイント設定の構築
    cache_point = {"type": "default"}
    if target_model in SUPPORTED_1H_TTL_MODELS:
        cache_point["ttl"] = "1h"

    system_prompt = [
        {"text": final_system_prompt_text},
        {"cachePoint": cache_point}
    ]
    return system_prompt, final_system_prompt_text


# 5. Bedrock APIを実行する関数
def invoke_bedrock(target_model, system_prompt, bedrock_messages, final_system_prompt_text, enable_grounding):
    # LLMへ渡される最終的なプロンプトとメッセージをログ出力
    # print("=== Sending to Bedrock ===")
    # print("--- SYSTEM PROMPT (COMBINED) ---")
    # print(final_system_prompt_text)
    # print("--- MESSAGES (PERSONA REMOVED) ---")
    # print(json.dumps(bedrock_messages, indent=2, ensure_ascii=False))
    # print("=========================")

    try:
        converse_params = {
            "modelId": target_model,
            "messages": bedrock_messages
        }
        
        if system_prompt:
            converse_params["system"] = system_prompt

        if enable_grounding:
            converse_params["toolConfig"] = {
                "tools": [{
                    "systemTool": {
                        "name": "nova_grounding"
                    }
                }]
            }

        response = bedrock_client.converse(**converse_params)
        return response, None
    except Exception as e:
        print("!!! BEDROCK INVOCATION ERROR !!!")
        print(str(e))
        return None, {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Bedrock invocation failed: {str(e)}"})
        }


# 6. OpenAI互換レスポンスを整形する関数
def format_openai_response(response, requested_model, target_model):
    output_message = response["output"]["message"]
    content_list = output_message.get("content", [])
    response_text = "".join([c["text"] for c in content_list if "text" in c])

    usage = response.get("usage", {})
    prompt_tokens = usage.get("inputTokens", 0)
    completion_tokens = usage.get("outputTokens", 0)
    total_tokens = prompt_tokens + completion_tokens

    # Bedrock側のキャッシュ利用状況を取得
    cache_read = usage.get("cacheReadInputTokens", 0)
    cache_write = usage.get("cacheWriteInputTokens", 0)
    
    # ログに出力
    print(f"--- Bedrock キャッシュ詳細 ---")
    print(f"通常入力トークン: {prompt_tokens}")
    print(f"出力トークン: {completion_tokens}")
    print(f"キャッシュ読み込み (節約分): {cache_read}")
    print(f"キャッシュ新規書き込み: {cache_write}")
    print(f"-----------------------------")

    openai_response = {
        "id": "chatcmpl-bedrock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens
        }
    }
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps(openai_response, ensure_ascii=False)
    }


# 3. メイン処理（唯一の lambda_handler）
def lambda_handler(event, context):
    global _prompts_loaded
    # 1. 認証とリクエスト検証
    body, error_res = parse_and_validate_request(event)
    if error_res:
        return error_res
        
    requested_model = body.get("model", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    openai_messages = body.get("messages", [])

    # 2. 対象モデルIDとWeb検索の判定
    target_model, enable_grounding, error_res = resolve_model_and_grounding(requested_model)
    if error_res:
        return error_res

    # コールドスタート対策: リクエスト内のプロファイル同期を行う前に、
    # メモリ上のキャッシュが空であればS3からロードして初期化しておく
    if not _prompts_loaded:
        load_all_prompts()

    # 3. 送信メッセージの構築 (NPCプロファイル同期・ペルソナ除去も実行)
    bedrock_messages = prepare_bedrock_messages(openai_messages)

    # 4. システムプロンプトのコンパイル
    system_prompt, final_system_prompt_text = get_compiled_system_prompt(target_model)

    # 5. Bedrock APIの実行
    bedrock_res, error_res = invoke_bedrock(
        target_model, 
        system_prompt, 
        bedrock_messages, 
        final_system_prompt_text, 
        enable_grounding
    )
    if error_res:
        return error_res

    # 6. レスポンスのOpenAI互換整形と返却
    return format_openai_response(bedrock_res, requested_model, target_model)