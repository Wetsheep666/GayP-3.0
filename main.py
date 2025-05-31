from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime

# 載入 .env 檔案
load_dotenv()

# 建立 Flask 應用
app = Flask(__name__)

# 初始化 LINE Bot API 和 WebhookHandler
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))

# 初始化 Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 使用者暫存狀態
user_states = {}

# 測試用 GET 路由
@app.route("/", methods=['GET'])
def home():
    return "LINE Bot is running."

# LINE Webhook 用 POST 路由
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# 處理 LINE 使用者訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "from"}
        reply = "請輸入出發地點："

    elif state.get("step") == "from":
        state["from"] = text.strip()
        state["step"] = "to"
        user_states[user_id] = state
        reply = "請輸入目的地點："

    elif state.get("step") == "to":
        state["to"] = text.strip()
        state["step"] = "time"
        user_states[user_id] = state
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："

    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            state["time"] = dt.isoformat()

            # 寫入 Supabase
            data = {
                "user_id": user_id,
                "origin": state["from"].strip(),
                "destination": state["to"].strip(),
                "time": state["time"]
            }
            insert_result = supabase.table("rides").insert(data).execute()
            print(f"[DEBUG] insert result: {insert_result}")

            # 查詢配對乘客（修正 datetime 比較）
            try:
                result = supabase.table("rides") \
                    .select("*") \
                    .eq("destination", state["to"].strip()) \
                    .neq("user_id", user_id) \
                    .execute()

                matched = []
                debug_lines = [f"[DEBUG] 總共找到 {len(result.data)} 位乘客："]

                for r in result.data:
                    try:
                        t1 = datetime.datetime.fromisoformat(state["time"]).replace(tzinfo=None)
                        t2 = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                        diff = abs((t1 - t2).total_seconds())
                        debug_lines.append(f"用戶 {r['user_id'][-5:]}, {r['origin']} → {r['destination']}, 時間: {r['time'][11:16]}, 差 {int(diff)}秒")
                        if diff <= 600:
                            matched.append(r)
                    except Exception as e:
                        debug_lines.append(f"時間格式錯誤: {e}")

                if matched:
                    match_lines = [
                        f"🚕 共乘對象：{r['user_id'][-5:]}, 時間：{r['time'][11:16]}" for r in matched
                    ]
                    match_text = "\n".join(match_lines)
                    reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n\n🧑‍🤝‍🧑 成功配對：\n{match_text}\n\n" + "\n".join(debug_lines)
                else:
                    reply = f"✅ 預約成功！\n從 {state['from']} 到 {state['to']}，時間 {text}\n\n目前暫無共乘對象。\n\n" + "\n".join(debug_lines)

            except Exception as e:
                reply = f"✅ 預約成功，但配對查詢失敗：{e}"

            user_states.pop(user_id)

        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："

    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            lines = [f"{r['origin']} → {r['destination']} 時間: {r['time'][11:16]}" for r in result.data]
            reply = "📋 你的預約如下：\n" + "\n".join(lines)
        else:
            reply = "你目前沒有任何預約。"

    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "🗑️ 所有預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」或「取消」來操作共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# Flask 本地啟動（Render 不會用到）
if __name__ == "__main__":
    app.run()
