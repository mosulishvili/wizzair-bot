import requests

TELEGRAM_BOT_TOKEN = "8782914659:AAFYct1Jp3ypWM3"
CHAT_ID = "898291410"

def send_wizzair_alert(flight_date, price_gel):
    message = (
        f"🚨 WIZZ AIR-ის ფასდაკლება! 🚨\n\n"
        f"📍 მარშრუტი: ქუთაისი ➔ ბარსელონა\n"
        f"📅 თარიღი: {flight_date}\n"
        f"💰 ფასი: {price_gel} ₾\n\n"
        f"🔗 https://wizzair.com"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("შეტყობინება გაიგზავნა!")
    else:
        print("შეცდომა:", response.text)

send_wizzair_alert("2026-10-15", "149")
