from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, LocationMessage
from dotenv import load_dotenv
from supabase import create_client, Client
from geopy.distance import geodesic
import os
import datetime

# 載入 .env 設定
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

# 處理地點
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
        reply = "📍 請傳送目的地（左下角「+」➜ 地點）"
    elif state.get("step") == "to":
        state.update({
            "to_address": address,
            "to_lat": lat,
            "to_lng": lng,
            "step": "time"
        })
        reply = "🕒 請輸入預約時間（格式：2025-06-01 18:00）："
    else:
        reply = "請先輸入「預約」開始流程。"

    user_states[user_id] = state
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.get(user_id, {})

    if text in ["預約", "我要搭車"]:
        profile = supabase.table("profiles").select("*").eq("user_id", user_id).execute().data
        if not profile:
            user_states[user_id] = {"step": "name"}
            reply = "👤 請先輸入您的姓名："
        else:
            user_states[user_id] = {"step": "from"}
            reply = "📍 請傳送出發地點（左下角「+」➜ 地點）"

    elif state.get("step") == "name":
        state["name"] = text
        state["step"] = "gender"
        reply = "👫 請輸入性別（男/女）："

    elif state.get("step") == "gender":
        if text not in ["男", "女"]:
            reply = "⚠️ 請輸入「男」或「女」"
        else:
            state["gender"] = text
            state["step"] = "phone"
            reply = "📞 請輸入聯絡電話（僅共乘對象會看到）："

    elif state.get("step") == "phone":
        state["phone"] = text
        state["step"] = "pet"
        reply = "🐶 是否會攜帶寵物？（是/否）"

    elif state.get("step") == "pet":
        state["has_pet"] = text == "是"
        state["step"] = "smoke"
        reply = "🚬 是否會吸菸？（是/否）"

    elif state.get("step") == "smoke":
        state["is_smoker"] = text == "是"
        state["step"] = "accept_pet"
        reply = "🐾 是否可接受對方攜帶寵物？（是/否）"

    elif state.get("step") == "accept_pet":
        state["accept_pet"] = text == "是"
        state["step"] = "accept_smoke"
        reply = "🚭 是否可接受對方吸菸？（是/否）"

    elif state.get("step") == "accept_smoke":
        state["accept_smoke"] = text == "是"
        supabase.table("profiles").upsert({
            "user_id": user_id,
            "name": state["name"],
            "gender": state["gender"],
            "phone": state["phone"],
            "pet_friendly": state["accept_pet"],
            "smoke_friendly": state["accept_smoke"],
            "is_smoker": state["is_smoker"],
            "has_pet": state["has_pet"]
        }).execute()
        state["step"] = "from"
        reply = "✅ 資料已儲存，請傳送出發地點（左下角「+」➜ 地點）"

    elif state.get("step") == "time":
        try:
            dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
            user_time = dt.replace(tzinfo=None)
            state["time"] = dt.isoformat()

            user_profile = supabase.table("profiles").select("*").eq("user_id", user_id).execute().data[0]
            supabase.table("rides").delete().eq("user_id", user_id).execute()
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

            candidates = supabase.table("rides").select("*").is_("matched_user", None).neq("user_id", user_id).execute().data
            match = None
            for r in candidates:
                try:
                    rt = datetime.datetime.fromisoformat(r["time"]).replace(tzinfo=None)
                    if abs((user_time - rt).total_seconds()) > 600:
                        continue
                    o_dist = geodesic((state["from_lat"], state["from_lng"]), (r["origin_lat"], r["origin_lng"])).meters
                    d_dist = geodesic((state["to_lat"], state["to_lng"]), (r["destination_lat"], r["destination_lng"])).meters
                    if o_dist > 1000 or d_dist > 1000:
                        continue

                    other_profile = supabase.table("profiles").select("*").eq("user_id", r["user_id"]).execute().data[0]
                    if user_profile["gender"] != other_profile["gender"]:
                        continue
                    if not user_profile["pet_friendly"] and other_profile["has_pet"]:
                        continue
                    if not user_profile["smoke_friendly"] and other_profile["is_smoker"]:
                        continue
                    if not other_profile["pet_friendly"] and user_profile["has_pet"]:
                        continue
                    if not other_profile["smoke_friendly"] and user_profile["is_smoker"]:
                        continue

                    match = r
                    break
                except Exception as e:
                    print("配對錯誤：", e)
                    continue

            if match:
                distance_km = geodesic((state["from_lat"], state["from_lng"]), (state["to_lat"], state["to_lng"])).km
                fare = max(50, int(distance_km * 50))
                share = fare // 2

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

                preview_link = f"https://www.google.com/maps/dir/?api=1&origin={state['from_lat']},{state['from_lng']}&destination={state['to_lat']},{state['to_lng']}&travelmode=driving"

                msg = f"✅ 配對成功！\n🧭 {state['from_address']} → {state['to_address']}\n💰 共乘費：${fare}，你需支付 ${share}\n☎️ 共乘對象電話：{other_profile['phone']}\n🗺️ 預覽路線：{preview_link}"
                line_bot_api.push_message(match["user_id"], TextSendMessage(text=msg))
                reply = msg
            else:
                reply = "✅ 預約成功，但目前沒有適合的共乘對象。"

            user_states.pop(user_id, None)

        except Exception as e:
            reply = f"⚠️ 時間格式錯誤或其他錯誤：{e}"

    elif text in ["查詢", "查詢預約"]:
        r = supabase.table("rides").select("*").eq("user_id", user_id).execute().data
        if r:
            r = r[0]
            reply = f"📋 預約資訊：\n{r['origin']} → {r['destination']}\n🕒 {r['time']}\n👥 配對對象：{r['matched_user'] or '尚未配對'}\n💰 你需支付：{r['share_fare'] or '待定'}"
        else:
            reply = "你目前沒有預約。"

    elif text in ["取消", "取消預約"]:
        supabase.table("rides").delete().eq("user_id", user_id).execute()
        user_states.pop(user_id, None)
        reply = "✅ 預約已取消。"

    else:
        reply = "請輸入：「預約」、「查詢」、「取消」開始使用共乘服務。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
