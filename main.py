from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
from geopy.distance import geodesic
import os
import datetime

# 載入 .env 參數
load_dotenv()

# 初始化 Flask 與 Supabase
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# 暫存使用者輸入
user_states = {}

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"
@handler.add(MessageEvent, message=LocationMessage)
def handle_location(event):
    user_id = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude
    address = event.message.address or "未知地點"
    state = user_states.get(user_id, {})

    if state.get("step") == "from":
        state.update({
            "from_address": address,
            "from_lat": lat,
            "from_lng": lng,
            "step": "to"
        })
        reply = "📍 請傳送目的地位置（建議點左下角「+」圖示 → 傳送位置）"
    elif state.get("step") == "to":
        state.update({
            "to_address": address,
            "to_lat": lat,
            "to_lng": lng,
            "step": "time"
        })
        reply = "🕒 請輸入搭車時間（例如：2025-06-01 18:00）："
    else:
        reply = "請輸入「預約」來開始。"

    user_states[user_id] = state
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    # 啟動預約流程
    if text in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "gender"}
        reply = "請輸入您的性別（男 / 女 / 其他）："

    # 第一步：輸入性別
    elif state.get("step") == "gender":
        state["gender"] = text
        state["step"] = "accept_pet"
        reply = "您是否接受共乘者攜帶寵物？（是 / 否）"

    # 第二步：是否接受寵物
    elif state.get("step") == "accept_pet":
        state["accept_pet"] = text == "是"
        state["step"] = "accept_smoke"
        reply = "您是否接受共乘者吸菸？（是 / 否）"

    # 第三步：是否接受吸菸
    elif state.get("step") == "accept_smoke":
        state["accept_smoke"] = text == "是"
        state["step"] = "from"
        reply = "請傳送出發地點（建議使用地圖 📍）"

    # 時間輸入 → 儲存預約 → 嘗試配對
    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["time"] = dt.isoformat()
            user_time = dt.replace(tzinfo=None)
            user_states.pop(user_id, None)

            # 刪除舊預約
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 新增預約
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
                "share_fare": None,
                "gender": state["gender"],
                "accept_pet": state["accept_pet"],
                "accept_smoke": state["accept_smoke"]
            }).execute()

            # 嘗試配對
            match = None
            origin = (state["from_lat"], state["from_lng"])
            destination = (state["to_lat"], state["to_lng"])
            results = supabase.table("rides").select("*").is_("matched_user", None).neq("user_id", user_id).execute().data

            for r in results:
                try:
                    r_time = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    if abs((user_time - r_time).total_seconds()) > 600:
                        continue
                    if geodesic(origin, (r["origin_lat"], r["origin_lng"])).meters > 1000:
                        continue
                    if geodesic(destination, (r["destination_lat"], r["destination_lng"])).meters > 1000:
                        continue
                    match = r
                    break
                except:
                    continue

            if match:
                # 計算價格
                km = geodesic(origin, destination).km
                fare = max(50, int(km * 50))
                share = fare // 2

                # 更新雙方資料
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

                # 預覽連結
                preview = f"https://www.google.com/maps/dir/?api=1&origin={state['from_lat']},{state['from_lng']}&destination={state['to_lat']},{state['to_lng']}&travelmode=driving"

                # 推送通知給對方
                line_bot_api.push_message(match["user_id"], TextSendMessage(
                    text=f"✅ 已與 {user_id} 配對成功！\n🧭 {match['origin']} → {match['destination']}\n💰 你需支付：${share}\n🗺️ 路線預覽：{preview}"
                ))

                reply = f"✅ 預約與配對成功！\n🧭 {state['from_address']} → {state['to_address']}\n💰 你需支付：${share}\n🗺️ 預覽路線：{preview}"
            else:
                reply = f"✅ 預約成功，但尚無符合條件的共乘對象。"

        except Exception as e:
            reply = f"⚠️ 請輸入正確時間格式（例如：2025-06-01 18:00）"

    elif text in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if result:
            r = result[0]
            reply = f"📋 預約資訊：\n出發：{r['origin']}\n目的：{r['destination']}\n時間：{r['time']}\n共乘對象：{r.get('matched_user') or '無'}\n💰 分攤費用：${r.get('share_fare') or '？'}"
        else:
            reply = "目前沒有預約資訊。"

    elif text in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "✅ 預約已取消。"

    else:
        reply = "請輸入「預約」、「查詢」、「取消」來開始使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
