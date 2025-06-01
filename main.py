from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, LocationMessage, TextSendMessage
from dotenv import load_dotenv
from supabase import create_client, Client
import os
import datetime
import requests
from geopy.distance import geodesic

load_dotenv()

app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

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
    state = user_states.get(user_id, {})
    lat, lng = event.message.latitude, event.message.longitude
    name = event.message.title or "地點"

    if state.get("step") == "from":
        state["from_lat"] = lat
        state["from_lng"] = lng
        state["from_name"] = name
        state["step"] = "to"
        user_states[user_id] = state
        reply = "請傳送目的地位置 📍"
    elif state.get("step") == "to":
        state["to_lat"] = lat
        state["to_lng"] = lng
        state["to_name"] = name
        state["step"] = "time"
        user_states[user_id] = state
        reply = "請輸入預約搭車時間（格式：2025-06-01 18:00）："
    else:
        reply = "請輸入「預約」開始共乘預約流程。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text.lower() in ["預約", "我要搭車"]:
        user_states[user_id] = {"step": "from"}
        reply = "請傳送出發地位置 📍"
    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            state["time"] = dt.isoformat()
            user_states[user_id] = state

            # 刪除舊資料
            supabase.table("rides").delete().eq("user_id", user_id).execute()

            # 計算總距離
            origin_coords = (state["from_lat"], state["from_lng"])
            dest_coords = (state["to_lat"], state["to_lng"])
            total_km = geodesic(origin_coords, dest_coords).km
            total_fare = max(25, int(total_km * 50))

            # 插入使用者資料
            supabase.table("rides").insert({
                "user_id": user_id,
                "origin": state["from_name"],
                "destination": state["to_name"],
                "time": state["time"],
                "matched_user": None,
                "fare": total_fare,
                "share_fare": None,
                "distance": total_km,
                "share_ratio": None,
                "from_lat": state["from_lat"],
                "from_lng": state["from_lng"],
                "to_lat": state["to_lat"],
                "to_lng": state["to_lng"]
            }).execute()

            # 嘗試配對
            candidates = supabase.table("rides").select("*") \
                .is_("matched_user", None) \
                .neq("user_id", user_id).execute()

            matched = None
            for c in candidates.data:
                try:
                    time_diff = abs((datetime.datetime.fromisoformat(c["time"]) - dt).total_seconds())
                    if time_diff <= 600:
                        # 比對地理距離
                        from_dist = geodesic(origin_coords, (c["from_lat"], c["from_lng"])).km
                        to_dist = geodesic(dest_coords, (c["to_lat"], c["to_lng"])).km
                        if from_dist <= 1 and to_dist <= 1:
                            matched = c
                            break
                except:
                    continue

            if matched:
                matched_dist = geodesic((matched["from_lat"], matched["from_lng"]),
                                        (matched["to_lat"], matched["to_lng"])).km
                share_ratio = total_km / (total_km + matched_dist)
                user_share = int(total_fare * share_ratio)
                other_share = total_fare - user_share

                supabase.table("rides").update({
                    "matched_user": matched["user_id"],
                    "share_ratio": round(share_ratio, 3),
                    "share_fare": user_share
                }).eq("user_id", user_id).execute()

                supabase.table("rides").update({
                    "matched_user": user_id,
                    "share_ratio": round(1 - share_ratio, 3),
                    "share_fare": other_share
                }).eq("user_id", matched["user_id"]).execute()

                reply = f"✅ 預約成功並成功配對！\n從 {state['from_name']} 到 {state['to_name']}，時間 {dt.strftime('%H:%M')}\n💰 你需支付：${user_share}"
            else:
                reply = f"✅ 預約成功！\n從 {state['from_name']} 到 {state['to_name']}，時間 {dt.strftime('%H:%M')}\n\n目前暫無共乘對象。"
            user_states.pop(user_id)
        except ValueError:
            reply = "⚠️ 時間格式錯誤，請重新輸入（例如：2025-06-01 18:00）："
    elif text.lower() in ["查詢", "查詢預約"]:
        result = supabase.table("rides").select("*").eq("user_id", user_id).execute()
        if result.data:
            lines = []
            for r in result.data:
                s = f"{r['origin']} → {r['destination']} 時間: {r['time']}"
                if r.get("matched_user"):
                    s += f"\n👥 共乘對象：{r['matched_user']}"
                if r.get("share_fare"):
                    s += f"\n💰 你需支付：${r['share_fare']}"
                lines.append(s)
            reply = "📋 你的預約如下：\n" + "\n\n".join(lines)
        else:
            reply = "你目前沒有任何預約。"
    elif text.lower() in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "🗑️ 所有預約已取消。"
    else:
        reply = "請輸入「預約」、「查詢」或「取消」來使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
