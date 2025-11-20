from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Form
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
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
app = FastAPI()
conversation_state = {}
active_calls: Dict[str, Dict] = {}
issues_store: List[CallData] = []
store_lock = asyncio.Lock()

@app.post("/voice")
async def start_call(CallSid: str = Form(...), From: str = Form(..., alias="From")):
    """Initial call handler - starts media stream"""
    if not is_e164(From) or is_rate_limited(From) or is_blocked(From):
        resp = VoiceResponse()
        resp.hangup()
        return Response(content=str(resp), media_type="application/xml")

    # Log the request for rate limiting
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
        'stream_sid': None
    }
    
    try:
        openai_ws = await connect_to_openai_realtime()
        await asyncio.gather(
            handle_twilio_to_openai(websocket, openai_ws, call_sid),
            handle_openai_to_twilio(openai_ws, websocket, call_sid),
            handle_openai_events(openai_ws, call_sid)
        )
        
    except WebSocketDisconnect:
        print(f"Call {call_sid} disconnected")
    except Exception as e:
        print(f"Error in call {call_sid}: {e}")
    finally:
        # Generate summary and cleanup
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
    openai_ws = await websockets.connect(openai_ws_url, extra_headers=headers)
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

    # Send initial greeting message
    greeting = "Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can ask to speak to our team at any time to register this as an urgent call. With that out the way, how may I help you today?"

    greeting_message = {
        "type": "response.create",
        "response": {
            "modalities": ["text", "audio"],
            "instructions": f"Start the conversation by saying: {greeting}"
        }
    }
    await openai_ws.send(json.dumps(greeting_message))

    return openai_ws

def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mulaw (8kHz) to PCM16 (24kHz)"""
    try:
        # decode mulaw to linear and resample from 8kHz to 24kHz
        pcm_8k = audioop.ulaw2lin(mulaw_data, 2)
        pcm_24k, _ = audioop.ratecv(
            pcm_8k,
            2,  # Sample width
            1,  # Channels
            8000,  # Input rate
            24000,  # Output rate
            None
        )
        return pcm_24k
    except Exception as e:
        print(f"Error converting mulaw to PCM16: {e}")
        return b''

def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert PCM16 (24kHz) to mulaw (8kHz)"""
    try:
        # resample from 24kHz to 8kHz
        pcm_8k, _ = audioop.ratecv(
            pcm_data,
            2,  # Sample width
            1,  # Channels
            24000,  # Input rate
            8000,  # Output rate
            None
        )
        mulaw_data = audioop.lin2ulaw(pcm_8k, 2)
        return mulaw_data
    except Exception as e:
        print(f"Error converting PCM16 to mulaw: {e}")
        return b''

async def handle_twilio_to_openai(twilio_ws: WebSocket, openai_ws, call_sid: str):
    """Forward caller's audio from Twilio to OpenAI"""
    try:
        async for message in twilio_ws.iter_text():
            try:
                data = json.loads(message)

                if data['event'] == 'start':
                    active_calls[call_sid]['stream_sid'] = data['start']['streamSid']
                    print(f"Stream started: {data['start']['streamSid']}")

                elif data['event'] == 'media':
                    audio_payload = data['media']['payload']
                    mulaw_audio = base64.b64decode(audio_payload)
                    pcm_audio = mulaw_to_pcm16(mulaw_audio)

                    if pcm_audio:  # Only send if conversion succeeded
                        audio_message = {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm_audio).decode('utf-8')
                        }
                        await openai_ws.send(json.dumps(audio_message))

                elif data['event'] == 'stop':
                    print(f"Stream stopped: {call_sid}")
                    break
            except json.JSONDecodeError as e:
                print(f"JSON decode error in Twilio message: {e}")
            except Exception as e:
                print(f"Error processing Twilio message: {e}")
    except Exception as e:
        print(f"Error in Twilio to OpenAI handler: {e}")

async def handle_openai_to_twilio(openai_ws, twilio_ws: WebSocket, call_sid: str):
    """Forward AI's audio from OpenAI to Twilio"""
    try:
        async for message in openai_ws:
            try:
                data = json.loads(message)

                if data['type'] == 'response.audio.delta':
                    pcm_audio = base64.b64decode(data.get('delta', ''))
                    mulaw_audio = pcm16_to_mulaw(pcm_audio)

                    stream_sid = active_calls[call_sid].get('stream_sid')
                    if stream_sid and mulaw_audio:  # Only send if we have stream_sid and valid audio
                        media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(mulaw_audio).decode('utf-8')
                            }
                        }
                        await twilio_ws.send_json(media_message)
                elif data['type'] == 'response.audio.done':
                    print("AI finished speaking")
            except json.JSONDecodeError as e:
                print(f"JSON decode error in OpenAI message: {e}")
            except Exception as e:
                print(f"Error processing OpenAI message: {e}")
    except Exception as e:
        print(f"Error in OpenAI to Twilio handler: {e}")

async def handle_openai_events(openai_ws, call_sid: str):
    """Handle OpenAI events like transcripts, function calls, etc."""
    current_response_text = ""

    try:
        async for message in openai_ws:
            try:
                data = json.loads(message)

                if data['type'] == 'conversation.item.input_audio_transcription.completed':
                    transcript = data.get('transcript', '')
                    if transcript and call_sid in active_calls:
                        active_calls[call_sid]['transcript'].append({
                            "role": "caller",
                            "message": transcript
                        })

                elif data['type'] == 'response.text.delta':
                    # Accumulate text deltas
                    text_delta = data.get('delta', '')
                    current_response_text += text_delta

                elif data['type'] == 'response.text.done':
                    # Save complete response when done
                    if current_response_text and call_sid in active_calls:
                        active_calls[call_sid]['transcript'].append({
                            "role": "bot",
                            "message": current_response_text
                        })
                        current_response_text = ""

                elif data['type'] == 'response.function_call_arguments.done':
                    function_name = data.get('name', '')
                    if function_name == "transfer_to_human":
                        await handle_transfer(call_sid)

                elif data['type'] == 'error':
                    error_msg = data.get('error', {})
                    print(f"OpenAI error: {error_msg}")

            except json.JSONDecodeError as e:
                print(f"JSON decode error in OpenAI events: {e}")
            except Exception as e:
                print(f"Error processing OpenAI event: {e}")
    except Exception as e:
        print(f"Error in OpenAI events handler: {e}")

async def handle_transfer(call_sid: str):
    """Transfer call to human using Twilio REST API"""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    client.calls(call_sid).update(
        url="http://twimlets.com/forward?PhoneNumber=+447873665370",
        method="POST"
    )

## POST CALL ACTIVITY - SUM TRANSCRIPTION FOR TICKET
async def finalize_call(call_sid: str):
    """Store transcript for /end_call webhook"""
    if call_sid in active_calls:
        async with store_lock:
            conversation_state[call_sid] = {
                'raw_transcript': active_calls[call_sid]['transcript'],
            }

@app.post("/end_call")
async def get_issue_type(CallSid: str = Form(...), From: str = Form("Unknown", alias="From"), CallStatus: str = Form("")):
    async with store_lock:
        print(f"Call {CallSid} ended with status {CallStatus}")
        state = conversation_state.get(CallSid, {})
        if not state or 'raw_transcript' not in state:
            print(f"No transcript found for {CallSid}")
            return {"status": "no_transcript"}

        # Convert transcript list to string for summary
        transcript_messages = [f"{msg['role']}: {msg['message']}" for msg in state['raw_transcript']]
        transcript_str = "\n".join(transcript_messages)

        summary = await generate_summary(transcript_str)
        raw_transcript = [ message["message"] for message in state['raw_transcript'] ]
        state['issue'] = CallData(
            name=summary.get('name', From),
            company=summary.get('company', 'no company information'),
            number=From,
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
async def poll(authorized: bool = Depends(verify_api_key)):
    """Existing poll endpoint - unchanged"""
    async with store_lock:
        issues_out = issues_store.copy()
        for issue in issues_store:
            issue.visited = True
        clear_old_issues(issues_store)
    return {"issues": issues_out}
