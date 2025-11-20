from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Form, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
import json
import base64
import asyncio
import websockets
import audioop
from typing import Dict, List
import os
from datetime import datetime
from aux import (
    is_e164, is_rate_limited, is_blocked, log_request,
    CallData, SYSTEM_PROMPT, generate_summary,
    clear_old_issues, verify_api_key
)

app = FastAPI()

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Global state
conversation_state = {}
active_calls: Dict[str, Dict] = {}
issues_store: List[CallData] = []
store_lock = asyncio.Lock()

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": "ok", "service": "BasicCaller WebSocket"}

@app.post("/voice")
async def start_call(request: Request):
    """Initial call handler - starts media stream"""
    form = await request.form()
    CallSid = form.get("CallSid")
    From = form.get("From")

    if not CallSid or not From:
        resp = VoiceResponse()
        resp.say("Sorry, there was an error processing your call.")
        resp.hangup()
        return Response(content=str(resp), media_type="application/xml")

    if not is_e164(From) or is_rate_limited(From) or is_blocked(From):
        resp = VoiceResponse()
        resp.hangup()
        return Response(content=str(resp), media_type="application/xml")

    log_request(From)
    async with store_lock:
        clear_old_issues(issues_store)

    resp = VoiceResponse()
    start = Start()
    stream = start.stream(
        url=f"wss://autoreceptionist.onrender.com/media-stream/{CallSid}",
        track="both_tracks"
    )
    resp.append(start)
    resp.pause(length=3600)

    return Response(content=str(resp), media_type="application/xml")

@app.websocket("/media-stream/{call_sid}")
async def media_stream_handler(websocket: WebSocket, call_sid: str):
    """Main WebSocket handler for realtime audio"""
    await websocket.accept()
    
    active_calls[call_sid] = {
        'ws': websocket,
        'transcript': [],
        'stream_sid': None,
        'connected': True
    }

    openai_ws = None
    try:
        openai_ws = await connect_to_openai_realtime()
        await asyncio.gather(
            handle_twilio_to_openai(websocket, openai_ws, call_sid),
            handle_openai_to_twilio_and_events(openai_ws, websocket, call_sid)
        )
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if call_sid in active_calls:
            active_calls[call_sid]['connected'] = False
        if openai_ws:
            try:
                await openai_ws.close()
            except:
                pass
        await finalize_call(call_sid)
        if call_sid in active_calls:
            del active_calls[call_sid]

async def connect_to_openai_realtime():
    """Connect to OpenAI's Realtime API via WebSocket"""
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    openai_ws = await websockets.connect(openai_ws_url, additional_headers=headers)
    session_config = {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": SYSTEM_PROMPT,  # Your Riley persona
            "voice": "alloy",
            "input_audio_format": "pcm16",  # Will convert from mulaw
            "output_audio_format": "pcm16",  # Will convert to mulaw
            "input_audio_transcription": {
                "model": "whisper-1"
            },
            "turn_detection": {
                "type": "server_vad",  # Voice Activity Detection
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 200
            }
        }
    }
    await openai_ws.send(json.dumps(session_config))

    greeting = "Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can ask to speak to our team at any time to register this as an urgent call. With that out the way, how may I help you today?"

    conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": greeting}]
        }
    }
    await openai_ws.send(json.dumps(conversation_item))

    response_create = {"type": "response.create"}
    await openai_ws.send(json.dumps(response_create))

    return openai_ws

def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mulaw (8kHz) to PCM16 (24kHz)"""
    try:
        pcm_8k = audioop.ulaw2lin(mulaw_data, 2)
        pcm_24k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 24000, None)
        return pcm_24k
    except:
        return b''

def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert PCM16 (24kHz) to mulaw (8kHz)"""
    try:
        pcm_8k, _ = audioop.ratecv(pcm_data, 2, 1, 24000, 8000, None)
        mulaw_data = audioop.lin2ulaw(pcm_8k, 2)
        return mulaw_data
    except:
        return b''

async def handle_twilio_to_openai(twilio_ws: WebSocket, openai_ws, call_sid: str):
    """Forward caller's audio from Twilio to OpenAI"""
    try:
        async for message in twilio_ws.iter_text():
            try:
                data = json.loads(message)
                if data['event'] == 'start':
                    active_calls[call_sid]['stream_sid'] = data['start']['streamSid']
                elif data['event'] == 'media':
                    mulaw_audio = base64.b64decode(data['media']['payload'])
                    pcm_audio = mulaw_to_pcm16(mulaw_audio)
                    if pcm_audio:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm_audio).decode('utf-8')
                        }))
                elif data['event'] == 'stop':
                    break
            except:
                pass
    except:
        pass

async def handle_openai_to_twilio_and_events(openai_ws, twilio_ws: WebSocket, call_sid: str):
    """Forward AI's audio from OpenAI to Twilio and handle events"""
    current_response_text = ""
    try:
        async for message in openai_ws:
            try:
                data = json.loads(message)
                if data['type'] == 'response.audio.delta':
                    delta = data.get('delta', '')
                    if delta:
                        pcm_audio = base64.b64decode(delta)
                        mulaw_audio = pcm16_to_mulaw(pcm_audio)
                        call_info = active_calls.get(call_sid, {})
                        stream_sid = call_info.get('stream_sid')
                        is_connected = call_info.get('connected', False)
                        if stream_sid and mulaw_audio and is_connected:
                            await twilio_ws.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(mulaw_audio).decode('utf-8')}
                            })
                elif data['type'] == 'conversation.item.input_audio_transcription.completed':
                    transcript = data.get('transcript', '')
                    if transcript:
                        call_data = active_calls.get(call_sid)
                        if call_data:
                            call_data['transcript'].append({"role": "caller", "message": transcript})
                elif data['type'] == 'response.text.delta':
                    current_response_text += data.get('delta', '')
                elif data['type'] == 'response.text.done':
                    if current_response_text:
                        call_data = active_calls.get(call_sid)
                        if call_data:
                            call_data['transcript'].append({"role": "bot", "message": current_response_text})
                        current_response_text = ""
                elif data['type'] == 'response.function_call_arguments.done':
                    if data.get('name') == "transfer_to_human":
                        await handle_transfer(call_sid)
            except:
                pass
    except:
        pass

async def handle_transfer(call_sid: str):
    """Transfer call to human using Twilio REST API"""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    client.calls(call_sid).update(
        url="http://twimlets.com/forward?PhoneNumber=+447873665370",
        method="POST"
    )

async def finalize_call(call_sid: str):
    """Store transcript for /end_call webhook"""
    if call_sid in active_calls:
        transcript = active_calls[call_sid].get('transcript', [])
        async with store_lock:
            conversation_state[call_sid] = {'raw_transcript': transcript if transcript else []}

@app.post("/end_call")
async def get_issue_type(request: Request):
    form = await request.form()
    CallSid = form.get("CallSid")
    From = form.get("From", "Unknown")

    async with store_lock:
        state = conversation_state.get(CallSid, {})
        if not state:
            return {"status": "no_state"}

        raw_transcript = state.get('raw_transcript', [])
        if not raw_transcript:
            return {"status": "no_transcript"}

        transcript_messages = [f"{msg['role']}: {msg['message']}" for msg in raw_transcript]
        transcript_str = "\n".join(transcript_messages)
        summary = await generate_summary(transcript_str)
        raw_transcript_text = [message["message"] for message in raw_transcript]

        issue_data = CallData(
            name=summary.get('name', From),
            company=summary.get('company', 'no company information'),
            number=From,
            system_info=summary.get('system_info', "no device information"),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=raw_transcript_text,
            visited=False,
            timestamp=datetime.utcnow()
        )
        issues_store.append(issue_data)
        return {"status": "saved"}

@app.get("/poll/")
async def poll(authorized: bool = Depends(verify_api_key)):
    """Existing poll endpoint - unchanged"""
    async with store_lock:
        issues_out = issues_store.copy()
        for issue in issues_store:
            issue.visited = True
        clear_old_issues(issues_store)
    return {"issues": issues_out}
