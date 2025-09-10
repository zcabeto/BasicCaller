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
company_names = ["ThreatSpike Labs"]
num_to_company = {"+447808289493": 0}

class CallData(BaseModel):
    name: str
    number: str
    company: str
    system_info: str
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
        action="https://basic-caller.onrender.com/device_info",
        method="POST",
        timeout=3
    )
    name_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/ask_name.mp3")
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
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

@app.post("/device_info")
def get_device_info(CallSid: str = Form(...), SpeechResult: str = Form(""), From: str = Form("Unknown")):
    """ask the caller for information about their system and location"""
    resp = VoiceResponse()

    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['number'] = From
        state['company'] = company_names[num_to_company[From]]
        state['name'] = SpeechResult if SpeechResult else state.get('name', "Caller")
        conversation_state[CallSid] = state
    
    # get system specs
    #resp.say(f"To help us narrow down the nature of your issue, please provide some information about the computer you are using and which location or office you are in.")
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/system_info.mp3")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/explain_issue",
        timeout=5
    )
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/explain_issue")
async def explain_issue(CallSid: str = Form(...), TranscriptionText: str = Form(""), From: str = Form("Unknown")):
    """pull out name and prompt for issue description"""
    resp = VoiceResponse()

    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['system_info'] = TranscriptionText if TranscriptionText else 'failed to record speech'
        conversation_state[CallSid] = state

    # ask for issue description
    #resp.say(f"Thank you. After the beep, please describe any issues you are having. Once you are done, please hang up and we will get back to you shortly with a call from our staff or an email showing a created ticket.")
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
        if not overwritten_issue_SID:    # initial call
            state = conversation_state.get(CallSid, {})
    
            summary = {
                "title": "Uncategorized Call",
                "description": TranscriptionText,
                "priority": "unknown"
            }
            #summary = await generate_summary(TranscriptionText)
            
            state['pending_issue'] = CallData(
                name=state.get('name', "Caller"),
                number=state.get('number', From),
                company=state.get('company', "Unknown Company"),
                system_info=state.get('system_info', "no device information"),
                title=summary['title'],
                description=summary['description'],
                priority=summary['priority'],
                raw_transcription=(TranscriptionText or "(empty)")
            )
            conversation_state[CallSid] = state

            # DO NOT CALL BACK
            issues_store.append(state['pending_issue'])
            conversation_state.pop(overwritten_issue_SID, None)
            return {"status": "saved"}
        else:    # transcription of second-try call
            state = conversation_state.get(overwritten_issue_SID, {})
            assert "pending_issue" in state
            state['pending_issue'].raw_transcription = TranscriptionText
            issues_store.append(state['pending_issue'])
            conversation_state.pop(overwritten_issue_SID, None)

    if TWILIO_NUMBER and From and Direction == "inbound":
        def initiate_callback():
            twilio_client.calls.create(
                to=From,
                from_=TWILIO_NUMBER,
                url=(
                    f"https://basic-caller.onrender.com/callback_summary"
                    f"?caller={quote_plus(state['pending_issue'].name)}"
                    f"&desc={quote_plus(state['pending_issue'].description)}"
                    f"&original_SID={CallSid}"
                )
            )
        background_tasks.add_task(initiate_callback)
    return {"status": "saved"}

## CHECK SUMMARY WITH CALLBACK (retired)
@app.post("/callback_summary")
async def callback_summary(original_SID: str = Query(...), caller: str = "", desc: str = ""):
    """Twilio fetches this when the user answers the callback"""
    resp = VoiceResponse()
    option_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action=f"https://basic-caller.onrender.com/confirm_issue?original_SID={original_SID}",
        method="POST",
        timeout=5
    )
    option_gather.say(
        f"<speak>Hello {unquote_plus(caller)}. We are calling you back to authenticate a call you recently placed registering an issue. <break time='1s'/>"
        f"We recorded your issue as the following: <break time='0.5s'/> {unquote_plus(desc)}. "
        "If this you made this call, please press 1. <break time='0.3s'/> If this summary is incorrect, press 2. <break time='0.3s'/>"
        "If you did not call us, press 3 to reject this call entirely.</speak>"
    )
    resp.say("We did not receive any input. Goodbye.")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/confirm_issue")
async def confirm_issue(original_SID: str = Query(...), Digits: str = Form(...)):
    """Confirm or reject the recorded issue"""
    resp = VoiceResponse()
    with store_lock:
        state = conversation_state.get(original_SID, {})
        pending_issue = state.get("pending_issue")
    if not pending_issue:
        resp.say("We could not find your issue. Goodbye.")
        resp.hangup()
        return Response(content=str(resp), media_type="text/xml")

    if Digits == "1":           # accept issue
        with store_lock:
            issues_store.append(pending_issue)
            conversation_state.pop(original_SID, None)
        resp.say("Thank you. Your issue has been recorded.")
        resp.hangup()
    elif Digits == "2":       # retry description
        resp.say("Okay, please describe your issue again after the beep.")
        resp.record(
            transcribe=True,
            transcribe_callback=f"https://basic-caller.onrender.com/transcription?overwritten_issue_SID={original_SID}",
            max_length=120,
            play_beep=True
        )
        resp.hangup()
    elif Digits == "3":        # discard
        with store_lock:
            conversation_state.pop(original_SID, None)
        resp.say("Your previous issue has been discarded. Goodbye.")
        resp.hangup()
    else:                      # wrong number entered
        resp.say("Invalid input. Press 1 to accept, 2 to re-record, or 3 to reject.")
        resp.gather(
            input="dtmf",
            num_digits=1,
            action=f"https://basic-caller.onrender.com/confirm_issue?original_SID={original_SID}",
            method="POST",
            timeout=5
        )
        resp.say("We did not receive any input. Goodbye.")
        resp.hangup()
    return Response(content=str(resp), media_type="text/xml")


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
