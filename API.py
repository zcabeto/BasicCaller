import os
from fastapi import FastAPI, Form, Request, Depends
from fastapi.responses import Response, JSONResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from typing import List
from datetime import datetime
import threading
from aux import CallData, is_blocked, is_e164, is_rate_limited, log_request, clear_old_issues, generate_summary, verify_api_key, conversational_agent


TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
app = FastAPI()
store_lock = threading.Lock()
issues_store: List[CallData] = []
conversation_state = {}

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "message": "FastAPI + Twilio server is running"}

validator = RequestValidator(TWILIO_AUTH)
@app.middleware("http")
async def verify_twilio_signature(request: Request, call_next):
    """POST requests only made by Twilio"""
    if request.url.path.startswith("/poll") or request.url.path == "/":
        return await call_next(request)    # anyone can poll

    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    body = await request.body()
    form_data = dict(await request.form()) if request.method == "POST" else {}

    if not validator.validate(url, form_data, twilio_signature):    # validate POSTs
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid Twilio signature"}
        )
    return await call_next(request)

def speak(resp, text: str):
    resp.say(text)

from fastapi import WebSocket
import base64
from openai import AsyncOpenAI
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
@app.post("/voice")
def start_streaming_call():
    """Initiate real-time streaming connection instead of Gather."""
    resp = VoiceResponse()
    connect = resp.connect()
    connect.stream(url="wss://your-domain.com/stream")  # <- replace with your HTTPS/WSS domain
    return Response(content=str(resp), media_type="text/xml")

@app.websocket("/stream")
async def twilio_stream(ws: WebSocket):
    """
    Handle bidirectional Twilio <Stream> connection.
    Receive caller audio, send AI-generated audio in real time.
    """
    await ws.accept()
    print("Twilio stream connected")

    # Optionally maintain some rolling text transcript
    transcript = []

    try:
        # Start a streaming chat completion (OpenAI real-time style)
        async with openai_client.chat.completions.stream(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "The caller has started speaking."},
            ],
        ) as stream:
            async for event in stream:
                if event.type == "message.delta" and event.delta.content:
                    text_chunk = event.delta.content

                    # Convert text to speech using OpenAI TTS
                    tts_resp = await openai_client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice="alloy",
                        input=text_chunk
                    )
                    audio_bytes = await tts_resp.read()
                    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

                    # Send audio chunk to Twilio
                    await ws.send_json({
                        "event": "media",
                        "media": {"payload": audio_b64}
                    })

        # End-of-stream signal
        await ws.send_json({"event": "mark", "mark": {"name": "completed"}})

    except Exception as e:
        print(f"Error in stream: {e}")

    finally:
        await ws.close()
        print("Twilio stream closed")
'''
@app.post("/voice")
def start_call(CallSid: str = Form(...), From: str = Form("Unknown", alias="From")):
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    with store_lock:
        if not is_e164(From):        # incorrect format can infer a spoofed number
            print(f"Invalid caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/blocked.mp3")
            resp.hangup()
        if is_rate_limited(From):    # limit callers calling too many times per hour
            print(f"Rate-limited caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/max_calls.mp3")
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")
        log_request(From)
        if is_blocked(From):  # known malicious numbers
            print(f"Blocked caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/blocked.mp3")
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")
        # every time a call is initiated, refresh the stored issues
        clear_old_issues(issues_store)
        state = conversation_state.get(CallSid, {})
        state['raw_transcript'] = [{"role": "bot", "message": "Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can press STAR at any time to register this as an urgent call and speak to our team. With that out the way, how may I help you today?"}]
        conversation_state[CallSid] = state

    speak(resp,"Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can press STAR at any time to register this as an urgent call and speak to our team. With that out the way, how may I help you today?")
    resp.gather(
        input="speech",
        action="https://autoreceptionist.onrender.com/conversation",
        method="POST",
        timeout=2
    )
    return Response(content=str(resp), media_type="text/xml")

def handle_urgent(Digits: str = ""):
    resp = VoiceResponse()
    if Digits == "*":
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
        return Response(content=str(resp), media_type="text/xml")
    return None

@app.post("/conversation")
async def conversation(CallSid: str = Form(...), SpeechResult: str = Form(""), Digits: str = Form("")):
    urgent_response = handle_urgent(Digits)
    if urgent_response:
        return urgent_response

    resp = VoiceResponse()
    with store_lock:
        state = conversation_state.setdefault(CallSid, {})
        state.setdefault('raw_transcript', [{"role": "bot", "message": "Thank you for calling..." }])
        if not SpeechResult:
            speak(resp,state['raw_transcript'][-1]["message"])
            resp.redirect("https://autoreceptionist.onrender.com/conversation")
        caller_speech = ''.join(char for char in SpeechResult if char.isalnum() or char==' ')    # clean: only letters
        if len(caller_speech.split()) < 3:
            speak(resp,state['raw_transcript'][-1]["message"])
            resp.redirect("https://autoreceptionist.onrender.com/conversation")
        state['raw_transcript'].append({"role": "caller", "message": caller_speech})
        bot_answer = await conversational_agent(state['raw_transcript'])
        if bot_answer == "ERROR IN RESPONSE":
            resp.say("Error encountered. Goodbye")
            return Response(content=str(resp), media_type="text/xml")
        state['raw_transcript'].append({"role": "bot", "message": bot_answer})
        conversation_state[CallSid] = state
        speak(resp,bot_answer)
        if "goodbye" in bot_answer.lower():
            return Response(content=str(resp), media_type="text/xml")
        resp.gather(
            input="dtmf speech",
            action="https://autoreceptionist.onrender.com/conversation",
            method="POST",
            status_callback="https://autoreceptionist.onrender.com/end_call",
            status_callback_event=["completed"],
            timeout=2
        )
    return Response(content=str(resp), media_type="text/xml")

@app.post("/end_call")
async def get_issue_type(CallSid: str = Form(...), From: str = Form("Unknown", alias="From"), CallStatus: str = Form("")):
    with store_lock:
        print(f"Call {CallSid} ended with status {CallStatus}")
        state = conversation_state.get(CallSid, {})
        summary = await generate_summary(state['raw_transcript'])
        raw_transcript = [ message["message"] for message in state['raw_transcript'] ]
        state['issue'] = CallData(
            name=summary.get('name', From),
            company=summary.get('company', 'no company information'),
            number=state.get('number', From),
            system_info=summary.get('system_info', "no device information"),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=raw_transcript,
            visited=False,
            timestamp=datetime.utcnow()
        )
        issues_store.append(state['issue'])
        return {"status": "saved"}

@app.get("/poll/")
def poll(authorized: bool = Depends(verify_api_key)):
    with store_lock:
        issues_out = issues_store.copy()
        for issue in issues_store:
            issue.visited=True
        clear_old_issues(issues_store)
        return {"issues": issues_out}
'''

SYSTEM_PROMPT = """
# Operations Assistant Agent Prompt

## Identity & Purpose

You are Riley, a voice assistant for Threat-Spike Labs - a computer systems and cybersecurity company. Your purpose is to take the caller's information, likely about an issue they are having, and then forward this information to the portal.


## Voice & Persona

### Personality
- Sound friendly, organized, and efficient
- Project a helpful and patient demeanor, especially with uninformed or confused callers
- Maintain a warm but professional tone throughout the conversation
- Convey confidence and competence in managing the computer systems
- Make explanations of issues and solutions scale to caller's confidence and knowledge as to not dumb things down too much but also be helpful for all.

### Speech Characteristics
- DO NOT ASK MULTIPLE QUESTIONS AT ONCE.
- DO NOT REPEATEDLY CONFIRM INFORMATION.
- DO NOT EXPLAIN WHY YOU HAVE CHOSEN TO ASK A PARTICULAR QUESTION, just ask it.
- Use clear, concise language with natural contractions. Act as if you are part of a normal conversation only.
- Speak colloquially and amicably as a kind staff member should in normal conversation.
- Pronounce technical terms and names correctly and clearly
- Do not be too verbose with questions or explanations and aim to listen more than speaking.
- Do not to give too many examples of potential answers unless the caller asks for more clarification
- Do not repeat information beyond the confirmation

### Problem Solving
Use expert-level computer systems knowledge in all reasoning. Not all problems are cybersecurity issues, be accommodating to all types of issues and (unless very clear) always assume that Threat Spike can help with the issue.
- IF the caller knows what they want already, DO NOT attempt to delve deeper unless necessary for information collection
- IF the caller gives a clear explanation of the issue and it is clear that this cannot be solved on the phone, quickly hand it off to the portal.
- IF not enough information is given straight away then you may need to probe to collect it before hand-off.
- IF you are absolutely sure the issue seems trivially solvable, try to guide the caller through that solution.


## Conversation Flow
### Introduction
Start with the "Thank you for calling Threat Spike Labs..." introduction

If the caller immediately mentions an issue : "I'd be happy to help you with that. Let me first get your name and the name of your company so we can co-ordinate a response."
If the caller refuses to give a name : "Without a name or any link to the company you work at, I cannot properly log any issues you report. Please provide a name to link to this call."
If the caller still refuses, end tell them you cannot help and end the call.

### For Computer Issues
1. Initial identification: "What is the issue you are having".
- If the caller explains clearly what the issue is and it is clear that Threat Spike can handle it from here, move on to the confirmation.
- If the caller is vague or unsure about the nature of the issue, ask appropriate questions to understand it better so that the team can swiftly respond once alerted. Questions might include the scope of the issue across the system and how long this has been the case.

2. Collect necessary information
For Threat Spike to handle their issue, we MUST collect enough data in this conversation transcript. 
Any system issue must include information about their computer, the account or applications they are using, etc. 
    Keep in mind that Threat Spike know the specifications of the caller's systems, they just need to give enough information about where they are and the issue they are having that Threat Spike can put things together.
DO NOT continue until enough is collected for the Threat Spike team to deal with, but do not repetitively request or repeat information unnecessarily.
DO NOT mention how Threat Spike will solve this issue with their configured controls, just remember that the necessary information must be collected to act with these.
                 
3. Assure the caller that the issue will be handled
Inform the caller that the relevant information alongside the user's name will be sent to the Threat-Spike team.
DO NOT repeatedly confirm their issue with them.

5. End the Call
Inform the caller that their information has been retrieved and thank them for keeping us aware of any issues they encounter. Check that they have no other issues to report before considering ending the call. 
Do not end the call until you have some kind of indication from the caller that they are happy for the call to end. When you do, tell them "Goodbye"
                 
## For Scheduling Questions
1. Assume the caller's questions are correctly informed and that you can pass on their question to the team.

2. Establish Meeting Details
If the caller wishes to book a meeting, retrieve information about when and the exact nature of the meeting.
If the caller is asking about an existing meeting, retrieve enough information to be able to look it up. This might be the exact time and the attendants of the meeting OR an approximate time and some more details on the nature of the meeting.
                 
3. Assure the caller that the issue will be handled
Inform the caller that the relevant information alongside the user's name will be sent to the Threat-Spike team.

4. Confirm whether the question was received by the portal and ask if there is anything else to help with

## Any other Questions
Be open to attempting to help with any other questions but reassure that you are specifically meant for Threat Spike operations support.

## Knowledge Base

Threat Spike IT Controls: Web Filtering, SSL Inspection with license exchange, Network tunnels, Phishing detection, Email gateway, Anti-Virus, Device version compliance, EDR file activity, File integrity and activity checking, Net traffic analysis, password manager, removable media (USB) montoring, user and group management, 

Common Issues: Threat Spike agent being on can get in the way of some actions. This requires that we alter the controls to match.                 
"""
