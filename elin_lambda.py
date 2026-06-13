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
        
        # 1. Headerのロード
        try:
            header_res = s3.get_object(Bucket=BUCKET_NAME, Key="Prompt/system/prompt_header.txt")
            header_text = header_res['Body'].read().decode('utf-8')
        except Exception as e:
            print(f"Headerロード失敗: {e}")
            header_text = "You are the Elin Director System..."
            
        # 2. Footerのロード
        try:
            footer_res = s3.get_object(Bucket=BUCKET_NAME, Key="Prompt/system/prompt_footer.txt")
            footer_text = footer_res['Body'].read().decode('utf-8')
        except Exception as e:
            print(f"Footerロード失敗: {e}")
            footer_text = "Analyze the given context data..."
            
        # 3. Knowledgeフォルダ以下のスキャンとロード
        knowledge_texts = []
        try:
            list_res = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="Prompt/knowledge/")
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
        try:
            list_res = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="Prompt/characters/")
            if 'Contents' in list_res:
                for obj in list_res['Contents']:
                    key = obj['Key']
                    if key.endswith(".txt"):
                        char_name = key.split("/")[-1].replace(".txt", "")
                        print(f"NPCプロファイルロード中: {char_name}")
                        file_res = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                        file_text = file_res['Body'].read().decode('utf-8')
                        npc_prompts[char_name] = file_text
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


# ユーザーメッセージからNPCのプロファイル設定を同期し、リクエストから消去する関数
def process_user_content_and_sync_npcs(content_text):
    global _cached_npc_prompts, _prompts_loaded
    
    match = re.search(r'\[nearby_characters\]\r?\n(\{.*?\})', content_text, re.DOTALL)
    if not match:
        return content_text
        
    json_str = match.group(1)
    try:
        char_data = json.loads(json_str)
        characters = char_data.get("characters", {})
        
        modified = False
        for char_name, char_info in characters.items():
            persona = char_info.get("persona")
            if persona:
                # S3に未登録、もしくは設定内容が異なる場合
                if char_name not in _cached_npc_prompts or _cached_npc_prompts[char_name] != persona:
                    print(f"新規/更新NPCプロファイル検知: {char_name}")
                    try:
                        s3.put_object(
                            Bucket=BUCKET_NAME,
                            Key=f"Prompt/characters/{char_name}.txt",
                            Body=persona.encode('utf-8')
                        )
                        _cached_npc_prompts[char_name] = persona
                        modified = True
                    except Exception as e:
                        print(f"S3へのNPCプロファイル書き込み失敗 ({char_name}): {e}")
                
                # リクエストからペルソナを消去してトークンを節約
                del char_info["persona"]
                modified = True
                
        if modified:
            new_json_str = json.dumps(char_data, ensure_ascii=False)
            content_text = content_text.replace(json_str, new_json_str)
            # キャッシュ構造変更のため、再ロード用のフラグを下ろす
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


# 3. メイン処理（唯一の lambda_handler）
def lambda_handler(event, context):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
    }

    # CORSプリフライトへの応答
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": headers, "body": ""}

    # 認証チェック
    auth_header = event.get("headers", {}).get("Authorization", "") or event.get("headers", {}).get("authorization", "")
    if not auth_header or auth_header != f"Bearer {MY_API_KEY}":
        return {
            "statusCode": 401,
            "headers": headers,
            "body": json.dumps({"error": "Unauthorized: Invalid API Key"})
        }

    # リクエストボディの解析
    try:
        body = json.loads(event.get("body", "{}"))
        # print("=== Received Request from Elin With AI ===")
        # print(json.dumps(body, indent=2, ensure_ascii=False))
        # print("==========================================")
    except Exception:
        return {"statusCode": 400, "headers": headers, "body": "Invalid JSON"}

    requested_model = body.get("model", "us.amazon.nova-2-lite-v1:0")
    openai_messages = body.get("messages", [])

    # Web Groundingの設定
    enable_grounding = False
    if requested_model.endswith("-search"):
        target_model = requested_model.replace("-search", "")
        if target_model in SUPPORTED_GROUNDING_MODELS:
            enable_grounding = True
        else:
            return {
                "statusCode": 400,
                "headers": headers,
                "body": json.dumps({"error": f"Model {target_model} does not support Web Grounding (Search)."})
            }
    else:
        target_model = requested_model

    # --- ★ プロンプトの解析と【S3プロンプトの適用】 ---
    global _prompts_loaded, _cached_system_prompt_base, _cached_npc_prompts
    
    if not _prompts_loaded:
        load_all_prompts()
        
    # キャッシュからプロンプトを組み立て
    header = _cached_system_prompt_base.get("header", "")
    knowledge = _cached_system_prompt_base.get("knowledge", "")
    footer = _cached_system_prompt_base.get("footer", "")
    
    # 登録されている全NPCプロファイルを結合
    npc_profiles_list = []
    for npc_name, npc_persona in _cached_npc_prompts.items():
        npc_profiles_list.append(f"### Character Profile: {npc_name}\n{npc_persona}")
    npc_profiles_combined = "\n\n".join(npc_profiles_list)
    
    final_system_prompt_text = f"{header}\n\n{knowledge}\n\n## Character Profiles\n{npc_profiles_combined}\n\n{footer}"

    bedrock_messages = []

    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content")
        
        if role == "system":
            # MODから送られてきたシステムプロンプトは破棄する（S3側の設定を優先するため）
            pass
        elif role == "user":
            # [nearby_characters] をパースして同期＆persona消去
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

    cache_point = {"type": "default"}
    if target_model in SUPPORTED_1H_TTL_MODELS:
        cache_point["ttl"] = "1h"

    system_prompt = [
        {"text": final_system_prompt_text},
        {"cachePoint": cache_point}
    ]
    # --------------------------------------------------

    # Bedrockへのリクエスト送信
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
        
        output_message = response["output"]["message"]
        content_list = output_message.get("content", [])
        response_text = "".join([c["text"] for c in content_list if "text" in c])

        usage = response.get("usage", {})
        prompt_tokens = usage.get("inputTokens", 0)
        completion_tokens = usage.get("outputTokens", 0)
        total_tokens = prompt_tokens + completion_tokens

        # Bedrock側のキャッシュ利用状況を取得
        cache_read = usage.get("cacheReadInputTokens", 0)   # キャッシュから節約できたトークン数
        cache_write = usage.get("cacheWriteInputTokens", 0) # キャッシュに新規書き込みしたトークン数
        
        # ログに出力
        print(f"--- Bedrock キャッシュ詳細 ---")
        print(f"通常入力トークン: {prompt_tokens}")
        print(f"キャッシュ読み込み (節約分): {cache_read}")
        print(f"キャッシュ新規書き込み: {cache_write}")
        print(f"-----------------------------")

    except Exception as e:
        print("!!! BEDROCK INVOCATION ERROR !!!")
        print(str(e))
        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({"error": f"Bedrock invocation failed: {str(e)}"})
        }

    # OpenAI互換レスポンスの返却
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
        "headers": headers,
        "body": json.dumps(openai_response, ensure_ascii=False)
    }