/**
 * dsh-kugou-listen —— 设置页（Client 层）
 * ============================================================================
 * 进「设置」左边那一栏：label 就是导航里显示的字。
 * 数据走 Host 开的 /api/kugou-listen：GET 读、POST 存、GET /now 试听一下。
 * ============================================================================
 */
window.__ModuleLoader__.load({
  id: 'dsh-kugou-listen',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect, useCallback } = React

    const API = '/api/kugou-listen'

    const CSS = `
      [data-kl] { display: flex; flex-direction: column; gap: 14px; }
      [data-kl-row] { display: flex; align-items: center; gap: 10px; }
      [data-kl-label] { flex: 0 0 168px; font-size: 13px; opacity: .82; }
      [data-kl-hint] { font-size: 12px; opacity: .55; margin-top: 3px; }
      [data-kl-input] { flex: 1 1 auto; min-width: 0; padding: 6px 9px; border-radius: 7px;
        border: 1px solid rgba(128,128,128,.35); background: rgba(128,128,128,.10); color: inherit; font-size: 13px; }
      [data-kl-btn] { padding: 6px 14px; border-radius: 7px; border: 1px solid rgba(128,128,128,.35);
        background: rgba(128,128,128,.14); color: inherit; font-size: 13px; cursor: pointer; }
      [data-kl-btn][disabled] { opacity: .5; cursor: default; }
      [data-kl-danger] { border-color: rgba(200,80,60,.55); color: #c05a4a; }
      [data-kl-pre] { margin: 0; padding: 10px 12px; border-radius: 8px; font-size: 12px; line-height: 1.6;
        background: rgba(128,128,128,.10); white-space: pre-wrap; word-break: break-all; max-height: 220px; overflow: auto; }
      [data-kl-ok] { font-size: 12px; color: #3d9a54; }
      [data-kl-err] { font-size: 12px; color: #c05a4a; }
      [data-kl-title] { font-size: 13px; font-weight: 600; opacity: .9; }
      [data-kl-notice] { border: 1px solid rgba(200,140,60,.45); background: rgba(200,140,60,.10);
        border-radius: 8px; padding: 8px 12px; font-size: 12px; }
      [data-kl-notice-sum] { cursor: pointer; font-weight: 600; opacity: .9; }
      [data-kl-notice-body] { margin-top: 6px; line-height: 1.7; opacity: .82; }
      [data-kl-notice-body] p { margin: 4px 0; }
      [data-kl-warn] { font-size: 12.5px; font-weight: 600; color: #c9772f;
        border-left: 3px solid #c9772f; background: rgba(201,119,47,.10);
        padding: 6px 10px; border-radius: 0 6px 6px 0; }
    `

    const num = (v, fallback) => {
      const n = Number(v)
      return Number.isFinite(n) ? n : fallback
    }

    /** 数字框：允许暂时空着，别一删就弹回 0 */
    function NumInput({ value, onCommit, min, max, width }) {
      const [text, setText] = useState(value === undefined || value === null ? '' : String(value))
      useEffect(() => {
        setText(value === undefined || value === null ? '' : String(value))
      }, [value])
      return h('input', {
        'data-kl-input': '', type: 'text', inputMode: 'numeric',
        style: width ? { width } : undefined,
        value: text,
        onChange: (e) => {
          const t = e.target.value.replace(/[^0-9]/g, '')
          setText(t)
          if (t !== '') {
            let n = Number(t)
            if (min !== undefined && n < min) n = min
            if (max !== undefined && n > max) n = max
            onCommit(n)
          }
        },
        onBlur: () => {
          if (text === '') onCommit(min !== undefined ? min : 0)
        },
      })
    }

    function Panel() {
      const [cfg, setCfg] = useState(null)
      const [msg, setMsg] = useState('')
      const [err, setErr] = useState('')
      const [busy, setBusy] = useState('')
      const [now, setNow] = useState('')
      const [deps, setDeps] = useState(null)

      const load = useCallback(async () => {
        try {
          const r = await fetch(API + '/settings')
          const j = await r.json()
          if (j.ok) setCfg(j.settings)
          else setErr(j.error || '读不到设置')
        } catch (e) {
          setErr(String(e))
        }
      }, [])

      useEffect(() => { load() }, [load])

      // 检查依赖：进页面自动查一次，好让「打开下载页」那两个按钮一开始就在。
      // ⚠️ 必须放在「读设置中」那个提前 return 之前 —— 钩子顺序一变，整页就白屏。
      const checkDeps = async (quiet) => {
        setBusy('deps'); if (!quiet) { setMsg(''); setErr('') }
        try {
          const r = await fetch(API + '/deps')
          const j = await r.json()
          setDeps(j)
          if (!quiet && j.deps && j.deps.ok) setMsg('依赖齐了')
        } catch (e) {
          if (!quiet) setErr('检查依赖失败：' + String(e))
        }
        setBusy('')
      }

      useEffect(() => { checkDeps(true) }, [])

      const save = async () => {
        setBusy('save'); setMsg(''); setErr('')
        try {
          const r = await fetch(API + '/settings', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(cfg),
          })
          const j = await r.json()
          if (j.ok) { setCfg(j.settings); setMsg('存好了，下一轮就按这个走') }
          else setErr(j.error || '存失败')
        } catch (e) {
          setErr(String(e))
        }
        setBusy('')
      }

      const probe = async () => {
        setBusy('now'); setMsg(''); setErr('')
        try {
          const r = await fetch(API + '/now')
          const j = await r.json()
          setNow(j.out || j.err || '(空)')
        } catch (e) {
          setErr(String(e))
        }
        setBusy('')
      }

      const check = async () => {
        setBusy('check'); setMsg(''); setErr('')
        try {
          const r = await fetch(API + '/selfcheck')
          const j = await r.json()
          const c = j.check
          if (c && c.steps) {
            const lines = c.steps.map((s) => `${s.ok ? '✓' : '✗'} ${s.name}：${s.detail}`)
            const verdict = c.verdict === 'ok' ? '能用' : (c.verdict === 'partial' ? '只能读一半' : '用不了')
            if (c.advice && c.advice.length) lines.push('', '怎么办：', ...c.advice.map((a) => '· ' + a))
            setNow(`结论：${verdict}\n\n` + lines.join('\n'))
          } else {
            setNow(j.out || j.err || '(空)')
          }
        } catch (e) {
          setErr(String(e))
        }
        setBusy('')
      }

      if (!cfg) {
        return h('div', { 'data-kl': '' },
          h('div', { 'data-kl-title': '' }, '酷狗一起听'),
          h('div', { 'data-kl-hint': '' }, err || '读设置中…'))
      }

      const set = (k, v) => setCfg({ ...cfg, [k]: v })

      return h('div', { 'data-kl': '' },
        h('div', { 'data-kl-title': '' }, '酷狗一起听'),
        h('div', { 'data-kl-hint': '' }, '让 agent 知道你在听什么、唱到哪一句；要不要接别人发来的一起听邀请。'),

        h('div', {
          'data-kl-warn': '',
          style: { fontSize: '15px', fontWeight: 700, padding: '10px 12px' },
        }, '⚠️ 这版功能可能有问题。用起来不对的话，别自己折腾，直接叫你家的 DSH 给你修。'),

        h('details', {
          'data-kl-notice': '',
        },
          h('summary', { 'data-kl-notice-sum': '' }, '用前必看（点开）'),
          h('div', { 'data-kl-notice-body': '' },
            h('p', null, '一、这套只认 Windows 上的酷狗桌面版。手机端、网页版、网易云都不在这条路上。'),
            h('p', null, '二、酷狗窗口不要最小化。最小化有可能抓到黑图，读不到东西。'),
            h('p', null, '三、歌词要停在歌曲页的「歌词」标签。停在歌单或搜索页时，只读得到歌名。'),
            h('p', null, '四、认的是默认皮肤和默认布局。换皮肤、改字号、改窗口比例，可能切歪，读出来是乱码或空。'),
            h('p', null, '五、屏幕缩放不是 100% 时坐标可能偏，这一条自检会给结论。'),
            h('p', null, '六、默认读的是屏幕画面；只有开了「真正的听力能力」才会录音。屏幕上盖别的窗口没关系，但酷狗必须真的在放。'),
            h('p', null, '七、想让它真的听：打开下面的「真正的听力能力」，填 API Key。不开就只能读屏。'),
            h('p', null, '八、默认只写本地日志（~/.kugou-listen/listening.log）。想推到手机就填 webhook；有 qq-bridge 就加 qqbridge 通道。'),
            h('p', null, '九、第一次装完要重启一次 DSH 才出现这一页；之后改这些设置会立刻生效，不用再重启。'),
            h('p', null, '十、遇到读不到，先点「自检」，它会告诉你卡在哪一步。'),
            h('p', null, '十一、开房、邀请好友、退出房间都能做：走系统的 UI 自动化，按控件名字点按钮，不靠截图认字猜坐标。前提是装了 uiautomation 这个 python 包（requirements.txt 里有）；没装就只能读歌读词，点不了按钮。'),
            h('p', null, '十二、退出房间会弹一层确认框，那一步也会自动点；如果你刚好在动鼠标，可能点空，重试一次即可。'))),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '邀请怎么处理'),
          h('select', {
            'data-kl-input': '',
            value: cfg.invite || 'ask',
            onChange: (e) => set('invite', e.target.value),
          },
            h('option', { value: 'ask' }, '先问我一句（ask）'),
            h('option', { value: 'auto' }, '看到就接（auto）'),
            h('option', { value: 'off' }, '不管（off）'))),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '隔多少秒看一眼'),
          h('div', { style: { flex: '1 1 auto', minWidth: 0 } },
            h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
              h('input', {
                type: 'checkbox', title: '开/关这一路扫描',
                checked: cfg.watch_on !== false,
                onChange: (e) => set('watch_on', e.target.checked),
              }),
              h(NumInput, {
                width: '100%', min: 5, max: 300,
                value: cfg.poll_seconds,
                onCommit: (v) => set('poll_seconds', v),
              })),
            h('div', { 'data-kl-hint': '' },
              '（左边勾选框是这一路的总开关，关掉就完全不扫描。右框决定 Ta 接受邀请的速度：数值越低处理越快，但消耗的 token 更多。作者建议：你发邀请的时候，顺手主动跟 Ta 说一声。）'),
            h('div', { 'data-kl-warn': '' },
              '⚠️ 改这个间隔需要重启你的 DSH 才会生效（刷新页面不算）。'))),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '看到邀请先发消息问'),
          h('input', {
            type: 'checkbox', checked: !!cfg.notify,
            onChange: (e) => set('notify', e.target.checked),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '把歌词一句句推出去'),
          h('input', {
            type: 'checkbox', checked: !!cfg.push_lyric,
            onChange: (e) => set('push_lyric', e.target.checked),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '推词最小间隔（秒）'),
          h(NumInput, {
            min: 30, max: 3600,
            value: cfg.push_interval,
            onCommit: (v) => set('push_interval', v),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '投递通道'),
          h('input', {
            'data-kl-input': '', type: 'text',
            placeholder: 'log, webhook, qqbridge',
            value: (cfg.deliver || []).join(', '),
            onChange: (e) => set('deliver', e.target.value.split(/[,，\s]+/).filter(Boolean)),
          })),

        h('div', { 'data-kl-warn': '' },
          '⚠️ 强烈建议把 DSH 绑定到 QQ 桥接（通道里加上 qqbridge）。' +
          '一起听的邀请走的是「复制链接」这条路，链接只能经由桥接送到你手机上；' +
          '没绑桥接的话，Ta 的邀请链接只会停在这台电脑的剪贴板里，你在手机上收不到。'),
        h('div', { 'data-kl-hint': '' },
          '（我相信，你都让 Ta 听歌了，估计也绑了桥接吧。）'),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, 'webhook 地址'),
          h('input', {
            'data-kl-input': '', type: 'text',
            placeholder: 'https://…（Bark / 钉钉 / Telegram 都吃）',
            value: cfg.webhook_url || '',
            onChange: (e) => set('webhook_url', e.target.value),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, 'QQ 收件人'),
          h('input', {
            'data-kl-input': '', type: 'text',
            placeholder: '选 qqbridge 通道时填：收件人的 QQ 号',
            value: cfg.owner_qq || '',
            onChange: (e) => set('owner_qq', e.target.value.replace(/\D/g, '')),
          })),

        h('div', { 'data-kl-title': '', style: { marginTop: '10px' } }, 'Ta 的听歌想法推送间隔'),
        h('div', { 'data-kl-hint': '' },
          '这个间隔只决定「Ta 多久冒一次话」。到点它会醒来看一眼你在放什么，换歌就先听一段，然后说一句自己的话。跟歌词推送不是一回事（那个是 push_lyric，默认关着）。'),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '开启'),
          h('input', {
            type: 'checkbox', checked: cfg.think_on !== false,
            onChange: (e) => set('think_on', e.target.checked),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, 'Ta 每隔多少分钟说一句'),
          h('div', { style: { flex: '1 1 auto', minWidth: 0 } },
            h(NumInput, {
              width: '100%', min: 1, max: 120,
              value: cfg.think_every_min,
              onCommit: (v) => set('think_every_min', v),
            }),
            h('div', { 'data-kl-hint': '' },
              '（填几就几分钟一次，不拦。代价照实说：1 分钟≈一天 1440 次模型调用，5 分钟≈288 次，10 分钟≈144 次。没在放歌的那些轮不会说话，但每轮也算一次调用。）'))),

        h('div', { 'data-kl-hint': '' },
          '不看钟点。判据只有一条：你在放歌，它才出声；没放就一句都不发。换歌时会立刻说，同一首则按上面的间隔。'),

        h('div', {
          'data-kl-warn': '',
        }, '⚠️ 改完不是立刻生效：定时任务会在下一轮开始时自己对齐到你填的分钟数（最多等当前一轮）。'),

        h('div', {
          'data-kl-warn': '',
        }, '⚠️ 没有歌在播放时，Ta 可能会给你发一条空消息（定时任务照样会跑一轮）。不喜欢就对 Ta 说：关闭查看酷狗音乐的自动任务。'),

        h('div', { 'data-kl-title': '', style: { marginTop: '10px' } }, '怎么让它真的开口'),
        h('div', { 'data-kl-hint': '' },
          '插件本身不会自己说话：说话要调模型，得由 Ta 那边的定时任务来跑。装好之后，把下面这句话原样发给 Ta，它就会自己把定时建起来。'),
        h('div', { 'data-kl-row': '' },
          h('button', {
            'data-kl-btn': '',
            onClick: async () => {
              const line =
                `从现在起，每 ${(cfg && cfg.think_every_min) || 10} 分钟看一眼酷狗在放什么（用 kugou_now）。` +
                '没在放歌、或者歌和上一句一样，就什么都不发，一个字都别输出。' +
                '换歌了可以用 kugou_hear 听一小段当作材料，然后说一句你自己的想法，用 kugou_say 发给我。' +
                '不许提模型、不许写「旋律很美」这类、不许和之前说过的重复、不带句号、一行一句。' +
                '把这套做成每期都要跑的定时任务。'
              try {
                await navigator.clipboard.writeText(line)
                setMsg('这句话已复制，粘给 Ta 就行')
              } catch (e) { setErr('复制失败，手动选下面的字') }
            },
          }, '复制这句话'),
          h('span', { 'data-kl-hint': '', style: { margin: 0 } }, '（发给 Ta，它会自己建定时）')),
        h('pre', { 'data-kl-pre': '' },
          `从现在起，每 ${(cfg && cfg.think_every_min) || 10} 分钟看一眼酷狗在放什么（用 kugou_now）。没在放歌就什么都不发。换歌了可以用 kugou_hear 听一小段当材料，然后说一句你自己的想法，用 kugou_say 发给我。不许提模型、不许写「旋律很美」、不许重复、不带句号。把这套做成定时任务。`),

        h('div', { 'data-kl-title': '', style: { marginTop: '10px' } }, '开启真正的听力能力'),
        h('div', { 'data-kl-hint': '' },
          '开了之后，换歌时它会录一小段音频交给多模态模型听——听的是歌词里没有的东西：怎么唱的、快慢、乐器、情绪。不开就只能读屏幕。'),
        h('div', { 'data-kl-hint': '' },
          '主模型没有声音模态时可以开这个（目前 DeepSeek 没有声音模态，所以开着才有用）。'),
        h('div', { 'data-kl-warn': '' },
          '⚠️ 开启后会增加 Ta 所发感想与你的听歌延迟：录一段要十几秒，识别又要几秒，所以那句感想会比歌慢一拍才到。'),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '开启'),
          h('input', {
            type: 'checkbox', checked: !!cfg.hearing,
            onChange: (e) => set('hearing', e.target.checked),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, 'API Key'),
          h('input', {
            'data-kl-input': '', type: 'password',
            placeholder: 'sk-…（只存在本机 ~/.kugou-listen/settings.json）',
            value: cfg.audio_api_key || '',
            onChange: (e) => set('audio_api_key', e.target.value),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, 'API URL'),
          h('input', {
            'data-kl-input': '', type: 'text',
            placeholder: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
            value: cfg.audio_api_url || '',
            onChange: (e) => set('audio_api_url', e.target.value),
          })),

        h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-label': '' }, '模型选择'),
          h('input', {
            'data-kl-input': '', type: 'text',
            placeholder: 'qwen-audio-turbo',
            value: cfg.audio_model || '',
            onChange: (e) => set('audio_model', e.target.value),
          })),

        h('div', { 'data-kl-row': '' },
          h('button', { 'data-kl-btn': '', disabled: busy === 'save', onClick: save }, '保存'),
          h('button', { 'data-kl-btn': '', disabled: busy === 'now', onClick: probe }, '试一下'),
          h('button', { 'data-kl-btn': '', disabled: busy === 'check', onClick: check }, '自检'),
          h('button', { 'data-kl-btn': '', disabled: busy === 'deps', onClick: checkDeps }, '检查依赖'),
          msg ? h('span', { 'data-kl-ok': '' }, msg) : null,
          err ? h('span', { 'data-kl-err': '' }, err) : null),

        now ? h('pre', { 'data-kl-pre': '' }, now) : null,

        // 依赖那块：不管装没装，两个按钮都常驻，省得别人找不到下载页
        deps && deps.deps ? h('div', { 'data-kl-row': '' },
          h('div', { 'data-kl-title': '', style: { marginTop: '10px' } }, '依赖'),
          (function () {
            const d = deps.deps
            const missing = d.missing || []
            const noUia = missing.includes('uiautomation')
                                 || (d.versions && !d.versions.uiautomation)
            const uiaVer = (d.versions && d.versions.uiautomation) || ''
            const rows = [
              h('div', { 'data-kl-hint': '' },
                `python ${d.python || ''}　${d.ok ? '依赖齐了' : '还缺东西'}`),
              h('div', { 'data-kl-hint': '' },
                noUia ? 'uiautomation：未安装（开房 / 邀请 / 退出房间要用它）'
                      : `uiautomation：已装 ${uiaVer}`),
            ]
            rows.push(h('div', { 'data-kl-row': '' },
              h('button', {
                'data-kl-btn': '',
                onClick: () => {
                  try { window.open('https://pypi.org/project/uiautomation/', '_blank', 'noopener') } catch (e) { setErr(String(e)) }
                },
              }, '打开 uiautomation 下载页'),
              h('button', {
                'data-kl-btn': '',
                onClick: async () => {
                  try {
                    await navigator.clipboard.writeText(deps.installCommand || '')
                    setMsg('安装命令已复制，粘到命令行里跑')
                  } catch (e) { setErr('复制失败，手动复制下面的命令') }
                },
              }, '复制安装命令')))
            if (noUia) {
              rows.push(h('div', { 'data-kl-warn': '' },
                '⚠️ 没装 uiautomation：读歌读词没问题，但开房、邀请、退出房间这些要点按钮的功能用不了。'))
            }
            if ((d.advice || []).length) {
              rows.push(h('div', { 'data-kl-hint': '' }, '怎么办：'))
              rows.push(h('pre', { 'data-kl-pre': '' }, d.advice.map((a) => '· ' + a).join('\n')))
            }
            return h('div', null, ...rows)
          })()) : null,

        h('div', { 'data-kl-hint': '' }, '通道说明：log 写本地 ~/.kugou-listen/listening.log；webhook 推一个地址；qqbridge 要有本地 QQ 桥接。'),
        h('div', { 'data-kl-hint': '' }, '建议：搭配 qq-bridge 使用效果更好 —— 能把话直接推到你 QQ 上，不用守着屏幕。'),
        h('div', { 'data-kl-hint': '' }, '制作：拾（其实是 DeepSeek）负责代码编写、坠雨已辞云（提供辅助）'),
        h('div', { 'data-kl-hint': '' }, '问题反馈请加 QQ：2876164958（坠雨已辞云）'))
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        const style = document.createElement('style')
        style.setAttribute('data-plugin-css', 'dsh-kugou-listen')
        style.textContent = CSS
        document.head.appendChild(style)
        ctx.effect(() => () => style.remove(), 'dsh-kugou-listen: 设置页样式')

        const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        if (!slots) return
        slots.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: 'kugou-listen', order: 33, label: '酷狗一起听' },
          () => h(Panel),
        ))
      },
    }
  },
})