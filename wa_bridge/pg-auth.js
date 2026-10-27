// WhatsApp linked-device session stored in PostgreSQL, so it survives container redeploys.
// Same format as Baileys' useMultiFileAuthState (one record per file), in a table instead of files.
//
// The session is a login credential for the WhatsApp number: anyone with this table can act as it.

import pg from 'pg'
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys'

const TABLE = 'wa_auth'

// Connect the way libpq (and the Python app) does by default: honour ?sslmode= when given,
// otherwise try a plain connection and switch to SSL if the server insists on it.
async function connectPool(url) {
  // Take the SSL options out of the URL so they don't override ours (plain string work: a password
  // with unusual characters would make URL parsing fail)
  const [address, query = ''] = url.split('?')
  const params = new URLSearchParams(query)
  const sslmode = (params.get('sslmode') || '').toLowerCase()
  for (const key of ['sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'uselibpqcompat']) params.delete(key)
  const connectionString = params.toString() ? `${address}?${params}` : address

  const encrypted = { rejectUnauthorized: false } // libpq "require": encrypted, certificate not verified
  const attempts =
    sslmode === 'disable' ? [false]
      : sslmode === 'verify-full' || sslmode === 'verify-ca' ? [true]
        : sslmode === 'require' ? [encrypted]
          : [false, encrypted] // prefer / not given

  let lastError
  for (const ssl of attempts) {
    const pool = new pg.Pool({ connectionString, ssl, max: 3, connectionTimeoutMillis: 15000 })
    try {
      await pool.query('SELECT 1')
      return pool
    } catch (e) {
      lastError = e
      await pool.end().catch(() => {})
    }
  }
  throw lastError
}

export async function usePostgresAuthState(url) {
  const pool = await connectPool(url)
  await pool.query(`CREATE TABLE IF NOT EXISTS ${TABLE} (id TEXT PRIMARY KEY, data TEXT NOT NULL)`)

  const read = async (id) => {
    const { rows } = await pool.query(`SELECT data FROM ${TABLE} WHERE id = $1`, [id])
    return rows.length ? JSON.parse(rows[0].data, BufferJSON.reviver) : null
  }
  const write = (id, value) =>
    pool.query(
      `INSERT INTO ${TABLE} (id, data) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data`,
      [id, JSON.stringify(value, BufferJSON.replacer)],
    )
  const remove = (id) => pool.query(`DELETE FROM ${TABLE} WHERE id = $1`, [id])

  const creds = (await read('creds')) || initAuthCreds()

  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {}
          await Promise.all(
            ids.map(async (id) => {
              let value = await read(`${type}-${id}`)
              if (type === 'app-state-sync-key' && value) {
                value = proto.Message.AppStateSyncKeyData.fromObject(value)
              }
              data[id] = value
            }),
          )
          return data
        },
        set: async (data) => {
          const tasks = []
          for (const category in data) {
            for (const id in data[category]) {
              const value = data[category][id]
              tasks.push(value ? write(`${category}-${id}`, value) : remove(`${category}-${id}`))
            }
          }
          await Promise.all(tasks)
        },
      },
    },
    saveCreds: () => write('creds', creds),
    clear: () => pool.query(`DELETE FROM ${TABLE}`),
  }
}
