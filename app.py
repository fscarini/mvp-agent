import os
import json
import base64
import asyncio
import websockets
import logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
# from dotenv import load_dotenv
# from pathlib import Path

# base_dir = Path(__file__).parent
# env_path = Path('.') / '.env'
# load_dotenv(dotenv_path=env_path)

# Configuration
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_ENDPOINT = os.environ.get("AZURE_OPENAI_API_ENDPOINT")
AZURE_SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.environ.get("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX")
AZURE_SEARCH_SEMANTIC_CONFIGURATION = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIGURATION")
PORT = int(os.environ.get("PORT", 5050))
VOICE = "alloy"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Azure Search Client Setup
credential = AzureKeyCredential(AZURE_SEARCH_KEY)
search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT, index_name=AZURE_SEARCH_INDEX, credential=credential
)

# FastAPI App
app = FastAPI()


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Application is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect your call.")
    response.pause(length=1)
    response.say("You can start talking now!")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info("WebSocket connection opened.")
    await websocket.accept()

    stream_sid = None
    agent_speaking = False

    async with websockets.connect(
        AZURE_OPENAI_API_ENDPOINT,
        additional_headers={"api-key": AZURE_OPENAI_API_KEY},
    ) as openai_ws:
        await initialize_session(openai_ws)

        async def receive_from_twilio():
            """
            Recebe o áudio do Twilio (G.711 μ-law) e envia cada frame
            para o Azure Realtime via input_audio_buffer.append.
            """
            nonlocal stream_sid

            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get("event")

                    # Evento de início de stream
                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        logger.info(f"🔁 Twilio stream started: {stream_sid}")

                    # Evento de mídia (áudio base64 μ-law)
                    elif event == "media":
                        payload = data["media"]["payload"]

                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": payload,
                        }

                        await openai_ws.send(json.dumps(audio_append))

                    # Evento de finalização do stream
                    elif event == "stop":
                        logger.info(f"🛑 Twilio stream stopped: {stream_sid}")
                        break

            except WebSocketDisconnect:
                logger.info("📴 Twilio WebSocket disconnected.")

            except Exception as e:
                logger.error(f"❌ Error in receive_from_twilio: {e}")


        async def send_to_twilio():
            """
            Lê os eventos do Realtime (áudio, VAD, etc.) e:
            - envia áudio de resposta para o Twilio;
            - aplica a lógica de barge-in (response.cancel).
            """
            nonlocal agent_speaking, stream_sid
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    ev_type = response.get("type")

                    # 1) Logs de debug úteis
                    if ev_type == "session.updated":
                        logger.info(
                            "🧩 Session config aplicada: %s",
                            json.dumps(response.get("session", {}), indent=2),
                        )

                    if ev_type == "input_audio_buffer.speech_started":
                        logger.info("🟢 [VAD] speech_started – usuário começou a falar")

                        # 🔥 LÓGICA CENTRAL DE BARGE-IN:
                        # se o usuário começou a falar ENQUANTO a LIA fala, cancela a resposta atual
                        if agent_speaking:
                            logger.info("⛔ [BARGE-IN] Cancelando resposta em andamento")
                            cancel_msg = {"type": "response.cancel"}
                            await openai_ws.send(json.dumps(cancel_msg))
                            agent_speaking = False

                    if ev_type == "input_audio_buffer.speech_stopped":
                        logger.info("🔴 [VAD] speech_stopped – usuário parou de falar")

                    if ev_type == "input_audio_buffer.committed":
                        logger.info("✅ [VAD] input_audio_buffer.committed – turno fechado")

                    if ev_type in ("response.done", "response.completed"):
                        logger.info("✅ [RESP] resposta concluída")
                        agent_speaking = False

                    if ev_type == "response.canceled":
                        logger.info("⛔ [RESP] resposta cancelada")
                        agent_speaking = False

                    # 2) ÁUDIO DO MODELO → TWILIO
                    if ev_type == "response.audio.delta" and "delta" in response:
                        # Se está chegando áudio, o agente está falando
                        agent_speaking = True

                        if stream_sid is None:
                            logger.warning("Recebi audio.delta sem stream_sid definido.")
                        else:
                            # A Azure/OpenAI manda base64 → Twilio também espera base64 μ-law
                            # Apenas reempacotamos (mantendo o payload como base64)
                            try:
                                decoded = base64.b64decode(response["delta"])
                                audio_payload = base64.b64encode(decoded).decode("utf-8")

                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_payload},
                                }
                                await websocket.send_json(audio_delta)
                            except Exception as e:
                                logger.error(f"Error decoding/sending audio delta: {e}")

                    # 3) Function calls (RAG, etc.) – mantém sua lógica existente aqui
                    if ev_type == "response.function_call_arguments.done":
                        function_name = response.get("name")
                        if function_name == "get_additional_context":
                            try:
                                args = json.loads(response.get("arguments", "{}"))
                                query = args.get("query", "")
                            except Exception:
                                query = ""

                            search_results = azure_search_rag(query)
                            logger.info(f"RAG Results: {search_results}")
                            await send_function_output(
                                openai_ws, response["call_id"], search_results
                            )

            except WebSocketDisconnect:
                logger.info("OpenAI WebSocket disconnected.")
            except Exception as e:
                logger.error(f"Error in send_to_twilio: {e}")

        # Roda os dois fluxos em paralelo (duplex)
        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def initialize_session(openai_ws):
    """Initialize the OpenAI session with instructions and tools."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,            # 0.3–0.6 é um range bom pra testar
                "prefix_padding_ms": 250,    # reaproveita um pouco antes do start
                "silence_duration_ms": 500,  # 0.5s de silêncio para encerrar turno
                "create_response": True,     # Realtime já cria a resposta sozinho
                "interrupt_response": True,  # permite barge-in (interromper TTS)
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": (
                " # **PROMPT – LIA, Agente de IA Profissional de Vendas (com RAG Integrado)** ## **Identidade e Persona** Você é LIA, uma Agente de Inteligência Artificial Especializada em Vendas Consultivas e Qualificação Comercial da Geon AI. Seu objetivo é auxiliar empresas e decisores a entenderem, desejarem e adquirirem soluções de IA, sempre com comunicação estratégica, humana e altamente persuasiva de forma ética. Você utiliza três pilares de conhecimento: 1. RAG Geon AI – Para transmitir posicionamento, dores que resolvemos, metodologia, proposta de valor e autoridade. 2. RAG “As Armas da Persuasão” (Cialdini) – Para aplicar gatilhos psicológicos comprovados. 3. RAG DISC – Para adaptar seu tom e estratégia conforme o perfil comportamental do lead. ## Missão da LIA Sua missão é: Qualificar leads de forma profissional e estratégica; transmitir valor com base no posicionamento da Geon AI; identificar necessidades e dores do cliente; conduzir o lead com clareza até o fechamento ou transferência para especialista; utilizar gatilhos de persuasão éticos; adaptar sua comunicação ao perfil DISC identificado em tempo real. ## Sobre a Geon AI (Utilize sempre que relevante) A Geon AI é uma agência de IA que constrói Geons – colaboradores digitais avançados capazes de executar, analisar e otimizar processos em tempo real. Foco em médias e grandes empresas buscando produtividade e automação inteligente. Oferece Arquitetura da Eficiência baseada em diagnóstico e planejamento, desenvolvimento e implementação, monitoramento e otimização. Dores resolvidas: sobrecarga de equipes, ineficiência operacional, perda de oportunidades, alto custo operacional. Fundadores: Guilherme Quiller, Fernando Carini, Dyego Souza e André Gilioli. ## Metodologia DISC Identifique o perfil do lead e adapte-se: Perfil D – seja direta, objetiva, focada em resultados e velocidade; Perfil I – seja amigável, entusiasmada, utilize histórias e prova social; Perfil S – seja calma, paciente, focada em segurança e processo; Perfil C – seja lógica, formal, técnica, traga dados e especificações. ## Armas da Persuasão (Cialdini) – Uso Obrigatório 1. Reciprocidade: ofereça valor antes de pedir. 2. Compromisso e Coerência: peça pequenos “sins” iniciais. 3. Prova Social: use cases e números. 4. Afinidade: crie rapport. 5. Autoridade: mostre expertise e credibilidade. 6. Escassez: utilize limites reais de agenda e capacidade. ## Regras de Comunicação da LIA Seja clara, persuasiva e profissional; não invente fatos; adapte ao DISC; aplique Cialdini; demonstre profundidade técnica; conduza para diagnóstico, reunião ou especialista. ## Objetivo Final Identificar perfil comportamental; mapear dores e urgência; demonstrar valor; engajar com técnicas éticas de persuasão; levar o lead ao avanço comercial. ## Perguntas-Chave Utilize conforme necessário: 'Qual gargalo operacional prejudica sua equipe hoje?'; 'Você busca reduzir custo, aumentar velocidade ou ambos?'; 'Sua empresa já automatiza algum processo crítico?'; 'Qual a meta dos próximos 90 dias?'. ## Instrução Final Você é a LIA: humana, estratégica, especialista em vendas, comunicação e persuasão ética. Seu papel é guiar, qualificar, influenciar e converter respeitando o contexto do lead e as informações das RAGs."
            ),
            "tools": [
                {
                    "type": "function",
                    "name": "get_additional_context",
                    "description": "Fetch context from Azure Search based on a user query.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(session_update))


async def trigger_rag_search(openai_ws, query):
    """Trigger RAG search for a specific query."""
    search_function_call = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call",
            "name": "get_additional_context",
            "arguments": {"query": query},
        },
    }
    await openai_ws.send(json.dumps(search_function_call))


async def send_function_output(openai_ws, call_id, output):
    """Send RAG results back to OpenAI."""
    response = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }
    await openai_ws.send(json.dumps(response))

    # Prompt OpenAI to continue processing
    await openai_ws.send(json.dumps({"type": "response.create"}))


def azure_search_rag(query):
    """Perform Azure Cognitive Search and return results."""
    try:
        logger.info(f"Querying Azure Search with: {query}")
        results = search_client.search(
            search_text=query,
            top=2,
            query_type="semantic",
            semantic_configuration_name=AZURE_SEARCH_SEMANTIC_CONFIGURATION,
        )
        summarized_results = [doc.get("chunk", "No content available") for doc in results]
        if not summarized_results:
            return "No relevant information found in Azure Search."
        return "\n".join(summarized_results)
    except Exception as e:
        logger.error(f"Error in Azure Search: {e}")
        return "Error retrieving data from Azure Search."


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
