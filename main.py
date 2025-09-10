from fastapi import FastAPI, Form, BackgroundTasks, Query
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List
import threading
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os
from openai import AsyncOpenAI
from urllib.parse import quote_plus, unquote_plus
import json

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()

class CallData(BaseModel):
    name: str
    number: str
    system_info: str
    issue_type: str
    title: str
    description: str
    priority: str
    raw_transcription: str

# thread-safe storage of issues over many calls, maintain conversation per call
store_lock = threading.Lock()
issues_store: List[CallData] = []
conversation_state = {}

@app.post("/voice")
def voice():
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    urgency_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://basic-caller.onrender.com/urgent_call",
        timeout=3
    )
    """urgency_gather.say(
        "Thank you for calling Threat Spike Labs! " \
        "If your call is urgent and you need to be handed to a member of staff, please press star. "
    )"""
    urgency_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/urgent_call.mp3")
    #resp.say("Your call has been registered as not urgent. Please start by providing your first and last name")
    name_gather = resp.gather(
        input="speech",
        action="https://basic-caller.onrender.com/issue_type",
        method="POST",
        timeout=3
    )
    name_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/ask_name.mp3")
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
    resp.redirect("https://basic-caller.onrender.com/voice")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/urgent_call")
def urgent_call(Digits: str = Form(...)):
    """only triggers if star (*) is pressed"""
    resp = VoiceResponse()
    if Digits == "*":
        #resp.say("Connecting you to a staff member now.")
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
    else:
        # note for later that this hangup just sends it back so we can make a loop with the conversation_state via this
        # just have to make the timeout a bit more dynamic but this works as an initial thought
        resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/issue_type")
def get_issue_type(CallSid: str = Form(...), SpeechResult: str = Form(""), From: str = Form("Unknown")):
    """Ask the caller to pick what type of issue they have"""
    resp = VoiceResponse()

    # Save caller info
    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['number'] = From
        state['name'] = SpeechResult if SpeechResult else state.get('name', "Caller")
        conversation_state[CallSid] = state

    # Ask for issue type
    issue_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://basic-caller.onrender.com/issue_resolve",
        timeout=5
    )
    issue_gather.say(
        "<speak>"
        "For computer or security issues, press 1. "
        "<break time='0.3s'/> For scheduling issues, press 2. "
        "<break time='0.3s'/> For general queries, press 3."
        "</speak>"
    )
    return Response(content=str(resp), media_type="text/xml")


@app.post("/issue_resolve")
def issue_resolve(Digits: str = Form(""), CallSid: str = Form(...)):
    """Handle the issue type chosen by the caller"""
    resp = VoiceResponse()

    if not Digits:  # timeout or no input
        resp.say("We did not receive any input.")
        resp.redirect("https://basic-caller.onrender.com/issue_type")
        return Response(content=str(resp), media_type="text/xml")

    # Save issue type
    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['issue_type'] = "systems" if Digits == "1" else ("scheduling" if Digits == "2" else "general")
        conversation_state[CallSid] = state
        print('input:', Digits, state['issue_type'])

    if Digits == "1":
        # System info: record asynchronously
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/system_info.mp3")
        resp.record(
            transcribe=True,
            transcribe_callback="https://basic-caller.onrender.com/explain_issue",
            max_length=30,
            play_beep=True,
            timeout=5,
            speech_timeout="auto"
        )
        # DO NOT hang up or redirect here
    elif Digits in ["2", "3"]:
        # Skip system info, go straight to main issue description
        resp.redirect("https://basic-caller.onrender.com/explain_issue")
    else:
        resp.say("Invalid input. Press 1 for computer issues, 2 for scheduling, or 3 for general queries.")
        resp.redirect("https://basic-caller.onrender.com/issue_type")

    return Response(content=str(resp), media_type="text/xml")


@app.post("/explain_issue")
async def explain_issue(CallSid: str = Form(...), TranscriptionText: str = Form(""), From: str = Form("Unknown")):
    """Handle system info transcription and ask for main issue description"""
    with store_lock:
        state = conversation_state.get(CallSid, {})

        # Save system info if the previous step was systems
        if state.get("issue_type") == "systems":
            state['system_info'] = TranscriptionText if TranscriptionText else "failed to record speech"
            conversation_state[CallSid] = state

    # Now ask for main issue description
    resp = VoiceResponse()
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/explain_issue.mp3")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/transcription",
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")


## GENERATE SUMMARY OF TRANSCRIPTION
async def execute_prompt(prompt: str):
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an assistant that extracts structured information from call transcripts."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
    except Exception as e:
        print(f"OpenAI error: {e}")
        return "fail"
    return resp.choices[0].message.content.strip()

async def generate_summary(transcription_text: CallData):
    prompt = f"""You are logging customer support phone calls. The customer has called and explained an issue.
    Caller transcription:
    "{transcription_text}"

    Please extract a short descriptive title (up to 8 words), a longer summary (1-3 sentences), and a priority level.
    Respond only with a JSON with the following format: 
        "title": "...",
        "description": "...",
        "priority": "urgent|high|medium|low|none"
    """
    default = {
        "title": "Uncategorized Call",
        "description": transcription_text,
        "priority": "unknown"
    }
    ai_result = default
    content = await execute_prompt(prompt)
    try:
        ai_output = json.loads(content)
        ai_result = ai_output
    except json.JSONDecodeError:
        ai_result = default
    required_keys = {"title", "description", "priority"}
    if not isinstance(ai_output, dict) or set(ai_output.keys()) != required_keys:
        return default
    return ai_result

## CREATE AND CHECK TRANSCRIPTION
@app.post("/transcription")
async def transcription(CallSid: str = Form(...), From: str = Form("Unknown"), TranscriptionText: str = Form(""), background_tasks: BackgroundTasks = None, Direction: str = Form("inbound"), overwritten_issue_SID: str = Query(None)):
    """create transcription and store the issue"""
    with store_lock:
        state = conversation_state.get(CallSid, {})

        summary = {
            "title": "Uncategorized Call",
            "description": TranscriptionText,
            "priority": "unknown"
        }
        #summary = await generate_summary(TranscriptionText)
        
        state['issue'] = CallData(
            name=state.get('name', "Caller"),
            number=state.get('number', From),
            system_info=state.get('system_info', "no device information"),
            issue_type=state.get('issue_type', 'system'),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=(TranscriptionText or "(empty)")
        )
        issues_store.append(state['issue'])
        return {"status": "saved"}


## ALLOW PULL FROM SERVER
@app.get("/poll/")
def poll():
    with store_lock:
        return {"issues": issues_store}

@app.get("/poll_and_clear/")
def poll_and_clear():
    with store_lock:
        issues = issues_store.copy()
        issues_store.clear()
    return {"issues": issues}
