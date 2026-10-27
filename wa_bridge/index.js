// WhatsApp bridge for the group agent.
//
// Links a dedicated WhatsApp number the way WhatsApp Web does (scan a QR code once),
// forwards every message it sees to the Python agent, and sends the agent's replies.
// The AI logic lives in the Python app (group_agent/); this process only moves messages.
//
// HTTP API (all calls need the X-Bridge-Secret header):
//   GET  /health   -> { connected, me }
//   GET  /qr       -> PNG of the current login QR code (404 once linked)
//   GET  /groups   -> [{ id, name, participants }]
//   POST /pair     -> { phone }  returns { code } to link via "Link with phone number" instead of the QR
//   POST /send     -> { chat, text, quoted?, mentions? }  sends a message, returns { id }
//                     mentions = phone numbers; write them in the text as @2348031234567

import http from 'node:http'
import { rm } from 'node:fs/promises'
import { timingSafeEqual } from 'node:crypto'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'
import { usePostgresAuthState } from './pg-auth.js'

const PORT = Number(process.env.BRIDGE_PORT || 3100)
const HOST = process.env.BRIDGE_HOST || '127.0.0.1'
const SECRET = process.env.BRIDGE_SECRET || ''
const AGENT_URL = process.env.AGENT_EVENTS_URL || 'http://127.0.0.1:3000/group-agent/events'
const AUTH_DIR = process.env.BRIDGE_AUTH_DIR || './wa_auth'
// With DATABASE_URL (PostgreSQL) the session is kept in the database and survives redeploys
const DATABASE_URL = /^postgres(ql)?:\/\//.test(process.env.DATABASE_URL || '') ? process.env.DATABASE_URL : null

if (!SECRET) {
  console.error('BRIDGE_SECRET is required')
  process.exit(1)
}

const logger = pino({ level: process.env.BRIDGE_LOG_LEVEL || 'warn' })
const log = (...args) => console.log(new Date().toISOString(), '[bridge]', ...args)

const IGNORED_KINDS = new Set([
  'protocolMessage', 'senderKeyDistributionMessage', 'messageContextInfo', 'keepInChatMessage',
  'pinInChatMessage', 'encReactionMessage', 'pollUpdateMessage',
])

let sock = null
let connected = false
let currentQr = null
const groupNames = new Map() // group jid -> subject

// ---------- WhatsApp connection ----------

async function loadAuth() {
  if (DATABASE_URL) {
    try {
      const auth = await usePostgresAuthState(DATABASE_URL)
      return { ...auth, where: 'PostgreSQL (wa_auth table)' }
    } catch (e) {
      // Never let a database problem stop the bridge: fall back to files (lost on redeploy)
      log('Could not use PostgreSQL for the session, falling back to files:', e.message)
    }
  }
  const auth = await useMultiFileAuthState(AUTH_DIR)
  return { ...auth, clear: () => rm(AUTH_DIR, { recursive: true, force: true }), where: AUTH_DIR }
}

let auth = null

async function connect() {
  if (!auth) {
    auth = await loadAuth()
    log('WhatsApp session stored in', auth.where)
  }
  const { state, saveCreds } = auth
  const { version } = await fetchLatestBaileysVersion()

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    // Must be a real browser name: WhatsApp rejects "Link with phone number" codes for custom names
    browser: Browsers.ubuntu('Chrome'),
    markOnlineOnConnect: false, // don't show "online" all day, like a normal phone
    syncFullHistory: false,
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      currentQr = qr
      log('Scan the QR code to link the number: GET /qr (or open wa_auth/qr.png)')
      await QRCode.toFile(`${AUTH_DIR}/qr.png`, qr, { width: 400 }).catch(() => {})
    }
    if (connection === 'open') {
      connected = true
      currentQr = null
      log('Connected as', sock.user?.id)
      refreshGroups().catch((e) => log('Could not load groups:', e.message))
    }
    if (connection === 'close') {
      connected = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        // Unlinked from the phone: forget the session and offer a fresh link (QR / pairing code)
        log('Logged out from the phone - clearing the session so it can be linked again')
        await auth.clear().catch((e) => log('Could not clear session:', e.message))
        auth = null
        setTimeout(() => connect().catch((e) => log('Reconnect failed:', e.message)), 5000)
        return
      }
      log('Connection closed (code', code, ') - reconnecting in 5s')
      setTimeout(() => connect().catch((e) => log('Reconnect failed:', e.message)), 5000)
    }
  })

  sock.ev.on('groups.update', (updates) => {
    for (const g of updates) if (g.id && g.subject) groupNames.set(g.id, g.subject)
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return // skip history sync, only live messages
    for (const msg of messages) {
      const event = await toEvent(msg)
      if (event) forward(event)
    }
  })
}

async function refreshGroups() {
  const groups = await sock.groupFetchAllParticipating()
  for (const g of Object.values(groups)) groupNames.set(g.id, g.subject)
  log(`In ${groupNames.size} group(s)`)
  return groups
}

async function groupName(jid) {
  if (!groupNames.has(jid)) {
    try {
      const meta = await sock.groupMetadata(jid)
      groupNames.set(jid, meta.subject)
    } catch {
      return null
    }
  }
  return groupNames.get(jid)
}

// Normalise a Baileys message into the plain event the Python agent expects
async function toEvent(msg) {
  const m = msg.message
  if (!m || !msg.key?.remoteJid) return null
  const chat = msg.key.remoteJid
  if (chat === 'status@broadcast') return null

  const content = m.ephemeralMessage?.message || m.viewOnceMessage?.message || m
  const text =
    content.conversation ||
    content.extendedTextMessage?.text ||
    content.imageMessage?.caption ||
    content.videoMessage?.caption ||
    content.documentMessage?.caption ||
    ''
  // Ignore WhatsApp's own plumbing (key distribution, sync, deletes); a group message's first
  // delivery carries a senderKeyDistributionMessage next to the real content, so skip that key
  const kind = Object.keys(content).find(
    (k) => (k.endsWith('Message') || k === 'conversation') && !IGNORED_KINDS.has(k),
  )
  if (!kind) return null
  const context = content.extendedTextMessage?.contextInfo || content.imageMessage?.contextInfo || {}
  const isGroup = chat.endsWith('@g.us')
  // Baileys 7 may identify people by a privacy id (@lid); prefer the phone-number form when given
  const sender = isGroup
    ? msg.key.participantAlt || msg.key.participant || ''
    : msg.key.remoteJidAlt || chat

  return {
    id: msg.key.id,
    chat,
    chat_name: isGroup ? await groupName(chat) : null,
    is_group: isGroup,
    sender,
    sender_name: msg.pushName || null,
    from_me: Boolean(msg.key.fromMe),
    type: kind.replace(/Message$/, '') || 'text',
    text,
    quoted_id: context.stanzaId || null,
    mentions: (context.mentionedJid || []).map((jid) => (isMe(jid) ? 'me' : jid)),
    timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
  }
}

// True for the assistant's own number or privacy id, so the agent knows when it is tagged
function isMe(jid) {
  const user = (id) => (id || '').split('@')[0].split(':')[0]
  return Boolean(jid) && [sock?.user?.id, sock?.user?.lid].some((id) => id && user(id) === user(jid))
}

async function forward(event) {
  try {
    const res = await fetch(AGENT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': SECRET },
      body: JSON.stringify(event),
    })
    if (!res.ok) log('Agent rejected event', event.id, res.status)
  } catch (e) {
    log('Agent unreachable:', e.message)
  }
}

// ---------- HTTP API for the agent ----------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function authorised(req) {
  const given = Buffer.from(req.headers['x-bridge-secret'] || '')
  const expected = Buffer.from(SECRET)
  return given.length === expected.length && timingSafeEqual(given, expected)
}

async function readJson(req) {
  let body = ''
  for await (const chunk of req) {
    body += chunk
    if (body.length > 100_000) throw new Error('body too large')
  }
  return JSON.parse(body || '{}')
}

function send(res, status, payload, type = 'application/json') {
  res.writeHead(status, { 'Content-Type': type })
  res.end(type === 'application/json' ? JSON.stringify(payload) : payload)
}

// The agent mentions people by phone number ("@2348031234567" in the text). Groups using privacy ids
// (@lid) know members by that id instead, so map each number to the id this group uses.
async function resolveMentions(chat, text, phones) {
  const digits = (phones || []).map((p) => String(p).replace(/\D/g, '')).filter(Boolean)
  if (!digits.length) return { text, mentions: [] }
  let participants = []
  if (chat.endsWith('@g.us')) {
    participants = (await sock.groupMetadata(chat).catch(() => null))?.participants || []
  }
  const mentions = []
  for (const phone of digits) {
    const member = participants.find((p) =>
      [p.phoneNumber, p.jid, p.id].some((id) => id && id.split('@')[0].split(':')[0] === phone))
    const jid = member?.id || `${phone}@s.whatsapp.net`
    mentions.push(jid)
    text = text.split(`@${phone}`).join(`@${jid.split('@')[0]}`)
  }
  return { text, mentions }
}

async function sendText(chat, text, quotedId, mentionPhones) {
  const resolved = await resolveMentions(chat, text, mentionPhones)
  text = resolved.text
  // Behave like a person: show "typing…" for a moment proportional to the length
  await sock.presenceSubscribe(chat).catch(() => {})
  await sock.sendPresenceUpdate('composing', chat).catch(() => {})
  await sleep(Math.min(8000, 1500 + text.length * 40))
  await sock.sendPresenceUpdate('paused', chat).catch(() => {})
  const options = quotedId
    ? { quoted: { key: { remoteJid: chat, id: quotedId }, message: { conversation: '' } } }
    : {}
  const content = resolved.mentions.length ? { text, mentions: resolved.mentions } : { text }
  const sent = await sock.sendMessage(chat, content, options)
  return sent?.key?.id
}

http
  .createServer(async (req, res) => {
    if (!authorised(req)) return send(res, 401, { error: 'unauthorised' })
    try {
      if (req.method === 'GET' && req.url === '/health') {
        return send(res, 200, {
          connected, me: sock?.user?.id || null, groups: groupNames.size, session: auth?.where || null,
        })
      }
      if (req.method === 'GET' && req.url === '/qr') {
        if (!currentQr) return send(res, 404, { error: connected ? 'already linked' : 'no QR yet' })
        return send(res, 200, await QRCode.toBuffer(currentQr, { width: 400 }), 'image/png')
      }
      if (req.method === 'GET' && req.url === '/groups') {
        if (!connected) return send(res, 503, { error: 'not connected' })
        const groups = await refreshGroups()
        return send(res, 200, Object.values(groups).map((g) => ({
          id: g.id, name: g.subject, participants: g.participants?.length || 0,
        })))
      }
      if (req.method === 'POST' && req.url === '/pair') {
        // Alternative to the QR code: WhatsApp > Linked devices > Link with phone number
        if (connected) return send(res, 409, { error: 'already linked' })
        const { phone } = await readJson(req)
        const digits = String(phone || '').replace(/\D/g, '')
        if (digits.length < 10) return send(res, 400, { error: 'phone must include the country code, e.g. 2348012345678' })
        if (!currentQr) return send(res, 503, { error: 'not ready yet - try again in a few seconds' })
        return send(res, 200, { code: await sock.requestPairingCode(digits) })
      }
      if (req.method === 'POST' && req.url === '/send') {
        if (!connected) return send(res, 503, { error: 'not connected' })
        const { chat, text, quoted, mentions } = await readJson(req)
        if (!chat || !text) return send(res, 400, { error: 'chat and text are required' })
        return send(res, 200, { id: await sendText(chat, text, quoted, mentions) })
      }
      send(res, 404, { error: 'not found' })
    } catch (e) {
      log('Request failed:', e.message)
      send(res, 500, { error: e.message })
    }
  })
  .listen(PORT, HOST, () => log(`Listening on ${HOST}:${PORT}, forwarding to ${AGENT_URL}`))

connect().catch((e) => {
  log('Could not start WhatsApp connection:', e.message)
  process.exit(1)
})
