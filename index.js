/**
 * dsh-kugou-listen —— Host 层
 * ============================================================================
 * 这一层干两件事：
 *   ① 把设置页要读写的那份配置落到 `~/.kugou-listen/settings.json`
 *      （采集那层是 python，它读 config.json + settings.json，后写的覆盖先写的）
 *   ② 给设置页开三个口：读设置 / 存设置 / 试听一下
 *
 * ⚠️ loader 可能把配置值包成占位对象（`{}`），所以只认基本类型，别的丢掉。
 * ============================================================================
 */
import { spawn, spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { loadHostDeps } from './lib/hostdeps.mjs'

const host = await loadHostDeps()
const z = host.z

const HERE = dirname(fileURLToPath(import.meta.url))
const HOME = join(homedir(), '.kugou-listen')
const UI_FILE = join(HOME, 'settings.json')
const ADV_FILE = join(HOME, 'config.json')
// 用哪个 python：装了 winocr 的那一个才算数。
// 顺序：环境变量 → ~/.kugou-listen/python.txt（setup.ps1 写的）→ PATH 里的 python。
// ⚠️ 不能无脑用 'python'：PATH 里那个常常没装 winocr，一调就 ModuleNotFoundError。
const PY_FILE = join(HOME, 'python.txt')

function usablePython(cmd) {
  try {
    const r = spawnSync(cmd, ['-c', 'import winocr'], { timeout: 15000, windowsHide: true })
    return r.status === 0
  } catch {
    return false
  }
}

function pickPython() {
  const cands = []
  if (process.env.KUGOU_LISTEN_PYTHON) cands.push(process.env.KUGOU_LISTEN_PYTHON)
  try {
    const saved = readFileSync(PY_FILE, 'utf8').trim()
    if (saved) cands.push(saved)
  } catch { /* 没写过就算了 */ }
  cands.push('python')

  for (const c of cands) {
    if (c && usablePython(c)) return c
  }
  return cands[cands.length - 1]        // 都不行就先返回最后一个，报错时能看到真实原因
}

const PY = pickPython()
const SCRIPT = join(HERE, 'tools', 'kugou_mcp.py')

const DEFAULTS = {
  invite: 'ask',
  watch_on: true,
  poll_seconds: 15,
  notify: true,
  push_lyric: false,
  push_interval: 90,
  deliver: ['log'],
  webhook_url: '',
  owner_qq: '',
  hearing: false,
  audio_api_key: '',
  audio_api_url: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
  audio_model: 'qwen-audio-turbo',
  think_every_min: 10,
  think_on: true,
}

const usable = (v) => {
  if (typeof v === 'boolean') return true
  if (typeof v === 'number' && Number.isFinite(v)) return true
  if (typeof v === 'string') return v !== ''
  if (Array.isArray(v)) return v.length > 0 && v.every((x) => typeof x === 'string')
  return false
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return {}
  }
}

function mergedSettings() {
  const out = { ...DEFAULTS }
  for (const f of [ADV_FILE, UI_FILE]) {
    for (const [k, v] of Object.entries(readJson(f))) {
      if (k in DEFAULTS && usable(v)) out[k] = v
    }
  }
  return out
}

function saveSettings(patch) {
  const next = mergedSettings()
  for (const [k, v] of Object.entries(patch || {})) {
    if (k in DEFAULTS && usable(v)) next[k] = v
  }
  mkdirSync(HOME, { recursive: true })
  writeFileSync(UI_FILE, JSON.stringify(next, null, 1), 'utf8')
  return next
}

function settingsFromConfig(config) {
  const out = {}
  for (const [k, v] of Object.entries(config || {})) {
    if (k in DEFAULTS && usable(v)) out[k] = v
  }
  return out
}

function runPython(args, timeout = 30000) {
  const r = spawnSync(PY, [SCRIPT, ...args], {
    encoding: 'utf8',
    timeout,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  })
  return {
    status: r.status ?? -1,
    stdout: String(r.stdout || ''),
    stderr: String(r.stderr || r.error || ''),
  }
}

function sendJson(res, code, body) {
  const text = JSON.stringify(body)
  res.statusCode = code
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.end(text)
}

/** 设置页上的字段。⭐ 每个都要 .extra('volatile', true) */
export const Config = !z ? undefined : z.object({
  invite: z.string().default('ask').extra('volatile', true),
  watch_on: z.boolean().default(true).extra('volatile', true),
  poll_seconds: z.natural().min(5).max(300).default(15).extra('volatile', true),
  notify: z.boolean().default(true).extra('volatile', true),
  push_lyric: z.boolean().default(false).extra('volatile', true),
  push_interval: z.natural().min(30).max(3600).default(90).extra('volatile', true),
  deliver: z.array(z.string()).default(['log']).extra('volatile', true),
  webhook_url: z.string().default('').extra('volatile', true),
  owner_qq: z.string().default('').extra('volatile', true),
  hearing: z.boolean().default(false).extra('volatile', true),
  audio_api_key: z.string().default('').extra('volatile', true),
  audio_api_url: z.string().default('https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation').extra('volatile', true),
  audio_model: z.string().default('qwen-audio-turbo').extra('volatile', true),
  think_on: z.boolean().default(true).extra('volatile', true),
  think_every_min: z.natural().min(1).max(120).default(10).extra('volatile', true),
})

export const inject = ['webServer']

export function apply(ctx, config) {
  try {
    const fromRow = settingsFromConfig(config)
    if (Object.keys(fromRow).length && !existsSync(UI_FILE)) saveSettings(fromRow)
  } catch { /* 写不动就算了 */ }

  ctx.effect(() => ctx.webServer.register({
    kind: 'prefix',
    path: '/api/kugou-listen',
    handler: async (req, res) => {
      try {
        const url = new URL(req.url, 'http://127.0.0.1')
        const path = url.pathname.replace(/^\/api\/kugou-listen/, '') || '/'

        if (req.method === 'GET' && path === '/settings') {
          return sendJson(res, 200, { ok: true, settings: mergedSettings() })
        }

        if (req.method === 'POST' && path === '/settings') {
          const chunks = []
          for await (const c of req) chunks.push(c)
          const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
          return sendJson(res, 200, { ok: true, settings: saveSettings(body) })
        }

        if (req.method === 'GET' && path === '/now') {
          const r = runPython(['--once'])
          return sendJson(res, 200, { ok: r.status === 0, out: r.stdout.trim(), err: r.stderr.trim() })
        }

        if (req.method === 'GET' && path === '/selfcheck') {
          const r = runPython(['--selfcheck'], 60000)
          let parsed = null
          try { parsed = JSON.parse(r.stdout.trim()) } catch { /* 下面用原始输出兜底 */ }
          return sendJson(res, 200, { ok: r.status === 0, check: parsed, out: r.stdout.trim(), err: r.stderr.trim() })
        }

        // 依赖体检：设置页那个「检查依赖」按钮用，顺便告诉它 uiautomation 装没装
        if (req.method === 'GET' && path === '/deps') {
          const r = runPython(['--deps'], 60000)
          let parsed = null
          try { parsed = JSON.parse(r.stdout.trim()) } catch { /* 兜底 */ }
          return sendJson(res, 200, {
            ok: r.status === 0,
            deps: parsed,
            python: PY,
            installCommand: `"${PY}" -m pip install pillow numpy winocr pyaudiowpatch uiautomation "mcp<2" -i https://pypi.tuna.tsinghua.edu.cn/simple`,
            out: r.stdout.trim(),
            err: r.stderr.trim(),
          })
        }

        // 2026-10-28：/restart 这条路由撤了 —— 从 DSH 内部去杀 DSH 自己，
        // 在桌面版上不可靠（会把自己的回合一起带走）。要重启就双击 工具\重启DSH.cmd。

        return sendJson(res, 404, { ok: false, error: 'no such route: ' + path })
      } catch (e) {
        return sendJson(res, 500, { ok: false, error: e?.message ?? String(e) })
      }
    },
  }), 'kugou-listen: /api/kugou-listen 路由')
}