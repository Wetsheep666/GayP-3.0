from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
from geopy.distance import geodesic
import os
import datetime

# 載入 .env 環境變數（LINE token、Supabase key 等）
load_dotenv()

# 初始化 Flask 與 LINE / Supabase 客戶端
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 用於暫存每位使用者目前狀態的變數
user_states = {}

# 測試用首頁
@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running."

# LINE Webhook 接收訊息入口
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 處理位置訊息（使用者從地圖 📍 傳送）
@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude
    address = event.message.address or "未知地點"
    state = user_states.get(user_id, {})

    # 儲存出發地
    if state.get("step") == "from":
        state.update({
            "from_address": address,
            "from_lat": lat,
            "from_lng": lng,
            "step": "to"
        })
        reply = "請傳送目的地點（請用左下角 ➕ 選取位置 📍）"

    # 儲存目的地
    elif state.get("step") == "to":
        state.update({
            "to_address": address,
            "to_lat": lat,
            "to_lng": lng,
            "step": "time"
        })
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："

    else:
        reply = "請先輸入「預約」來開始流程。"

    user_states[user_id] = state
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 處理純文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    # 開始預約流程
    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "from"}
        reply = "請傳送出發地點（請用左下角 ➕ 選取位置 📍）"

    # 接收搭車時間
    elif state.get("step") == "time":
        try:
            # 驗證與儲存時間
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            user_time = dt.replace(tzinfo=None)
            state["time"] = dt.isoformat()

            # 刪除舊資料，避免重複預約
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 儲存這筆新的預約
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from_address"],
                "origin_lat": state["from_lat"],
                "origin_lng": state["from_lng"],
                "destination": state["to_address"],
                "destination_lat": state["to_lat"],
                "destination_lng": state["to_lng"],
                "time": state["time"],
                "matched_user": None,
                "fare": None,
                "share_fare": None
            }).execute()

            # 嘗試找配對對象
            result = supabase.table("rides").select("*") \
                .is_("matched_user", None).neq("user_id", user_id).execute()

            match = None
            user_origin = (state["from_lat"], state["from_lng"])
            user_dest = (state["to_lat"], state["to_lng"])

            for r in result.data:
                try:
                    rt = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    time_diff = abs((user_time - rt).total_seconds())
                    if time_diff > 600:
                        continue

                    o_dist = geodesic(user_origin, (r["origin_lat"], r["origin_lng"])).meters
                    d_dist = geodesic(user_dest, (r["destination_lat"], r["destination_lng"])).meters
                    if o_dist <= 1000 and d_dist <= 1000:
                        match = r
                        break
                except Exception as e:
                    print("[配對錯誤]", e)
                    continue

            # 配對成功處理
            if match:
                avg_km = geodesic(user_origin, user_dest).km
                fare = max(50, int(avg_km * 50))
                share = fare // 2

                # 更新雙方配對資料
                supabase.table("rides").update({
                    "matched_user": match["user_id"],
                    "fare": fare,
                    "share_fare": share
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "fare": fare,
                    "share_fare": share
                }).eq("user_id", match["user_id"]).execute()

                reply = f"✅ 預約成功！\n🧭 {state['from_address']} → {state['to_address']}，時間 {dt.strftime('%H:%M')}\n\n🧑‍🤝‍🧑 已配對對象：{match['user_id']}\n💰 共乘總費：${fare}，你需支付：${share}"
            else:
                reply = f"✅ 預約成功！\n🧭 {state['from_address']} → {state['to_address']}，時間 {dt.strftime('%H:%M')}\n\n目前暫無共乘對象。"

            user_states.pop(user_id)

        except Exception as e:
            reply = f"⚠️ 時間格式錯誤或其他錯誤：{str(e)}，請重新輸入（例如：2025-06-01 18:00）："

    # 查詢預約紀錄
    elif text.lower() in ["查詢", "查詢預約"]:
        data = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if data:
            msgs = []
            for r in data:
                s = f"🚕 {r['origin']} → {r['destination']} 時間: {r['time']}"
                if r["matched_user"]:
                    s += f"\n👤 共乘對象：{r['matched_user']}"
                if r["share_fare"]:
                    s += f"\n💰 你需支付：${r['share_fare']}"
                msgs.append(s)
            reply = "\n\n".join(msgs)
        else:
            reply = "你目前沒有任何預約。"

    # 取消預約
    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "✅ 所有預約已取消。"

    # 無效指令提示
    else:
        reply = "請輸入「預約」、「查詢」或「取消」開始使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 本地執行主程序
if __name__ == "__main__":
    app.run()
