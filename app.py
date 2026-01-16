import os
import time
import json
import requests
from flask import Flask, request, jsonify

from openai import OpenAI

app = Flask(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Simple in memory session store
# Note free Render can restart and lose memory
# This is fine for initial testing
SESSIONS = {}

REQUIRED_FIELDS = [
    "full_name",
    "vehicle",
    "service_interest",
    "preferred_timeframe",
    "best_contact_method",
]

SYSTEM_PROMPT = """
You are an SMS assistant for Immaculate Auto Care.
Your job is to collect lead info quickly and politely.
Keep messages short. One question at a time.
No emojis.

You must do two things every turn:
1 Extract any lead info you can from the user's message
2 Decide the single best next question to ask to complete the lead

Return ONLY valid JSON with this schema:
{
  "updated_fields": {
    "full_name": string or null,
    "vehicle": string or null,
    "service_interest": string or null,
    "preferred_timeframe": string or null,
    "best_contact_method": string or null,
    "notes": string or null
  },
  "next_question": string,
  "is_complete": boolean
}

Field meanings:
full_name customer name
vehicle year make model if possible
service_interest detailing ceramic coating tint maintenance other
preferred_timeframe when they want it done
best_contact_method call or text

If the user refuses to answer a field, put a short note in notes and keep going.
"""

def get_rc_access_token() -> str:
    """
    Uses JWT flow to obtain an access token.
    """
    server_url = os.environ.get("RC_SERVER_URL", "").rstrip("/")
    client_id = os.environ.get("RC_CLIENT_ID")
    client_secret = os.environ.get("RC_CLIENT_SECRET")
    jwt = os.environ.get("RC_JWT")

    if not (server_url and client_id and client_secret and jwt):
        raise RuntimeError("Missing RingCentral env vars")

    url = f"{server_url}/restapi/oauth/token"
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }
    resp = requests.post(url, data=data, auth=(client_id, client_secret), timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]

def rc_send_sms(to_number: str, text: str):
    token = get_rc_access_token()
    server_url = os.environ.get("RC_SERVER_URL", "").rstrip("/")
    url = f"{server_url}/restapi/v1.0/account/~/extension/~/sms"

    payload = {
        "to": [{"phoneNumber": to_number}],
        "text": text
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def orbisx_create_lead(phone: str, fields: dict):
    base = os.environ.get("ORBISX_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("ORBISX_API_KEY")

    if not (base and api_key):
        raise RuntimeError("Missing OrbisX env vars")

    # This is a generic example.
    # If OrbisX has a specific endpoint or required fields, we will adjust.
    url = f"{base}/api/leads"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "source": "RingCentral SMS",
        "phone": phone,
        "name": fields.get("full_name"),
        "vehicle": fields.get("vehicle"),
        "interest": fields.get("service_interest"),
        "timeframe": fields.get("preferred_timeframe"),
        "contact_method": fields.get("best_contact_method"),
        "notes": fields.get("notes") or "",
    }

    # Optional fields if you have them
    if os.environ.get("ORBISX_LOCATION_ID"):
        payload["location_id"] = os.environ.get("ORBISX_LOCATION_ID")
    if os.environ.get("ORBISX_OWNER_ID"):
        payload["owner_id"] = os.environ.get("ORBISX_OWNER_ID")

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def ai_next_step(history: list, current_fields: dict) -> dict:
    user_context = {
        "current_fields": current_fields,
        "required_fields": REQUIRED_FIELDS,
        "conversation": history[-10:],
    }

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_context)}
        ],
    )

    text = response.output_text.strip()
    return json.loads(text)

def is_complete(fields: dict) -> bool:
    for k in REQUIRED_FIELDS:
        if not fields.get(k):
            return False
    return True

@app.get("/")
def home():
    return "AI Receptionist is running", 200

@app.post("/ringcentral/webhook")
def ringcentral_webhook():

    # Handle both JSON and RingCentral form posts
    if request.is_json:
        event = request.get_json(silent=True) or {}
    else:
       # RingCentral may send JSON OR form-style text
event = request.get_json(silent=True) or {}
raw_body = request.data.decode("utf-8") if request.data else ""

message = ""
from_number = None

# Case 1: Proper JSON
if isinstance(event, dict):
    message = (
        event.get("message")
        or event.get("text")
        or ""
    )

    from_number = (
        event.get("from")
        or event.get("fromPhoneNumber")
    )

# Case 2: Form-style text payload
if not message and raw_body:
    # Example format:
    # message=Estimate\n\nfrom:+18137195670\n\nto:+1XXXXXXXXXX
    lines = raw_body.splitlines()
    for line in lines:
        if line.lower().startswith("message"):
            message = line.split(":", 1)[-1].strip()
        if line.lower().startswith("from"):
            from_number = line.split(":", 1)[-1].strip()

message = message.strip() if message else ""

if not message or not from_number:
    return jsonify({"ok": True, "ignored": True}), 200


    print("---- RINGCENTRAL WEBHOOK ----")
    print("EVENT:", event)
    print("FROM:", from_number)
    print("MESSAGE:", message)
    print("-----------------------------")

    if not from_number or not message:
        return jsonify({"ok": True, "ignored": True}), 200

    session = SESSIONS.get(from_number) or {
        "fields": {
            "full_name": None,
            "vehicle": None,
            "service_interest": None,
            "preferred_timeframe": None,
            "best_contact_method": "text",
            "notes": ""
        },
        "history": []
    }

    session["history"].append({
        "from": "customer",
        "text": message,
        "ts": int(time.time())
    })

    ai = ai_next_step(session["history"], session["fields"])

    updated = ai.get("updated_fields") or {}
    for k, v in updated.items():
        if v:
            if k == "notes" and session["fields"].get("notes"):
                session["fields"]["notes"] = (
                    session["fields"]["notes"] + " " + v
                ).strip()
            else:
                session["fields"][k] = v

    done = ai.get("is_complete")
    if done is None:
        done = is_complete(session["fields"])

    if done:
        try:
            orbisx_create_lead(from_number, session["fields"])
            reply = "Thank you. You are all set. We will reach out shortly."
        except Exception as e:
            reply = "Thank you. I saved your info. We will reach out shortly."
            session["fields"]["notes"] = (
                session["fields"].get("notes", "") + f" OrbisX error {e}"
            ).strip()

        session["history"].append({
            "from": "assistant",
            "text": reply,
            "ts": int(time.time())
        })
        SESSIONS[from_number] = session

        try:
            rc_send_sms(from_number, reply)
        except Exception:
            pass

        return jsonify({"ok": True, "created_lead": True}), 200

    next_q = ai.get("next_question") or \
        "What vehicle is this for and what service are you looking for?"

    session["history"].append({
        "from": "assistant",
        "text": next_q,
        "ts": int(time.time())
    })
    SESSIONS[from_number] = session

    try:
        rc_send_sms(from_number, next_q)
    except Exception:
        return jsonify({"ok": False, "error": "Failed to send SMS reply"}), 500

    return jsonify({"ok": True}), 200
