import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Activity, Archive, Brain, Check, CheckCircle2, ChevronLeft, ChevronRight,
  CircleCheck, Clock, Download, FileText, FolderOpen, Grid3X3, Home, Image,
  ImageOff, Inbox, LayoutGrid, Library, ListChecks, Maximize2, Mic,
  RefreshCw, Search, Settings, Sparkles, StopCircle, Trash2, Workflow,
  X, Zap,
} from 'lucide-react';

// Look-up table for stage icons so STAGES config can reference by name.
const STAGE_ICONS: Record<string, any> = {
  FileText, ListChecks, Sparkles, LayoutGrid, Archive, CircleCheck,
};
import {
  ReactFlow, ReactFlowProvider, MiniMap, Controls, Background, BackgroundVariant,
  type Node, type Edge, type NodeProps, Handle, Position,
} from '@xyflow/react';

type StageKey = 'ask_location'|'review_prompts'|'generating'|'review_frames'|'save'|'done';
type VoiceState = 'idle'|'listening'|'thinking'|'speaking';
interface Prompt {
  id: string;
  text: string;
  status: 'pending'|'approved'|'denied'|'rejected';
  title?: string;
  topic_number?: number | null;
  slide_number?: number | null;
  edited?: boolean;
}
interface Frame  {
  id: string; prompt_text: string; status: string;
  temp_url?: string; final_url?: string; final_path?: string;
  source?: 'lotus'|'manual'|'skipped'|null;  // provenance for per-frame credit in reports
  error?: string | null;                      // populated when status='failed'
  section?: string;                           // Post1..PostN, for label on upload tooltip
  slide_idx?: number;
  retry_count?: number;                       // ReviewAgent's 3-attempt retry counter
}
interface AgentHealth {
  name: string;
  alive: boolean;
  busy: number;
  success_count: number;
  fail_count: number;
  success_rate: number | null;    // null when no work has run yet
  last_error: string | null;
  remote?: boolean;               // D4 — true for RemoteAgent proxies
  host?: string;                  // "host:port" when remote=true
  recent_failures?: number;       // circuit-breaker penalty (0 = healthy)
  ping_fails?: number;            // D5 — consecutive probe misses
  last_ping_ok?: number | null;   // D5 — unix ts of last successful probe
}
interface ControllerAlert {
  id: number;          // client-side counter (dedup/animation key)
  ts: number;          // unix seconds
  level: 'info' | 'warning' | 'error';
  agent: string;       // which agent the alert is about ('Gemma' when it's a pattern-spotter alert)
  message: string;
}
interface PipelineSummary {
  id: string;
  name: string;
  stage: StageKey;
  paused: boolean;
  cancelled: boolean;
  frame_count: number;
  prompt_count: number;
  statuses: Record<string, number>;
  by_source: Record<string, number>;
  created_at?: number;
}
interface PipelineError {
  ts: number;                       // unix seconds
  frame_id: string;
  section: string;
  stage: string;                    // "generate" | "burst" | ...
  message: string;
}
interface PipelineState {
  id: string; name: string; stage: StageKey;
  source_md?: string;
  prompts: Prompt[]; frames: Frame[];
  save_section?: string; processing?: boolean; processing_label?: string; error?: string;
  created_at?: number | string;
  paused?: boolean;
  pause_reason?: string | null;
  errors?: PipelineError[];
  agents?: AgentHealth[];
}
interface LotusConfig {
  brand: string;
  parent_folder: string;
  parent_path: string;
  projects_root: string;
  multi_frame_sections: string[];
  single_item_slots: string[];
  frame_prefix: string;
  frame_ext: string;
  active_section: string;
  // Primary image-gen engine. Switchable live via POST /api/config/pipeline.
  // 'playwright' = gemini_bot.py (current default)
  // 'human'      = gemini_bot_human.py (OS-level pyautogui + AppleScript + CDP DOM)
  // 'api'        = gemini_api.py (official paid API)
  pipeline?: 'playwright' | 'human' | 'api';
}
interface ActivityItem { id: number; ts: number; kind: 'cmd'|'reply'|'stage'|'info'; text: string }
interface NewsItem { title: string; url: string; source: string; published: string }
interface NewsPayload { topic: string; items: NewsItem[]; error?: string }
interface ClaudeSuggestion { query: string; response: string; score: number; status: 'working'|'streaming'|'done'|'error' }
interface NewsAnalysis {
  title: string; url: string; analysis: string;
  status: 'working'|'done';
  has_article?: boolean; cursor?: number; total?: number;
  entities?: string[]; source?: string; published?: string; topic?: string;
}
interface PostReview {
  status: 'working'|'done';
  index: number; total: number;
  id?: string; section?: string; source?: string;
  prompt_text: string;
  analysis?: string; summary?: string; my_take?: string;
}
interface PostImprovement {
  status: 'working'|'done'|'accepted'|'rejected';
  index: number; total: number; id?: string;
  original: string; improved: string;
  changes: string[]; focus?: string;
}
interface PostImageGen {
  status: 'working'|'done'|'error';
  index: number; total: number; id?: string;
  prompt: string; section?: string; improved?: boolean;
  image_url?: string; image_path?: string;
  folder?: string; filename?: string; parent_folder?: string;
  expected_url?: string; expected_path?: string; expected_folder?: string;
  result?: string;
}

// Lazy-referenced here; actual components come from lucide-react (imported
// at top of file). Using a wrapper type so TS doesn't complain.
const STAGES: { key: StageKey; title: string; desc: string; IconKey: string; color: string }[] = [
  { key:'ask_location',   title:'Source',        desc:'Pick the markdown file with your prompts.',         IconKey:'FileText',      color:'cyan'   },
  { key:'review_prompts', title:'Prompts',       desc:'Verify each NB2 prompt, then click Start Generation.', IconKey:'ListChecks',    color:'amber'  },
  { key:'generating',     title:'Generate',      desc:'Gemini produces each frame from its prompt.',       IconKey:'Sparkles',      color:'violet' },
  { key:'review_frames',  title:'Review Frames', desc:'Approve, deny, or regenerate each image.',          IconKey:'LayoutGrid',    color:'violet' },
  { key:'save',           title:'Save',          desc:'Drop approved frames into the destination folder.', IconKey:'Archive',       color:'accent' },
  { key:'done',           title:'Complete',      desc:'All frames saved with the correct hierarchy.',      IconKey:'CircleCheck',   color:'green'  },
];

const COLOR_HEX: Record<string, string> = {
  cyan: '#00e5ff', amber: '#fbbf24', violet: '#a78bfa', accent: '#ff6b35', green: '#10b981',
};
const withAlpha = (hex: string, a: number) => {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0,2),16), g = parseInt(h.slice(2,4),16), b = parseInt(h.slice(4,6),16);
  return `rgba(${r},${g},${b},${a})`;
};

type NodeData = {
  stage: StageKey;
  state: 'pending' | 'active' | 'done';
  title: string; desc: string; iconKey: string; color: string;
  footer?: string;
  progress?: number;
  elapsed?: string;    // "3m 42s" for per-stage timer when active
  onClick?: () => void;
};

function StageNode({ data }: NodeProps<Node<NodeData>>) {
  const [hovered, setHovered] = useState(false);
  const pillText = data.state==='active' ? 'In Progress' : data.state==='done' ? 'Completed' : 'Pending';
  const accent = COLOR_HEX[data.color] || '#a78bfa';
  const isActive = data.state === 'active';
  const isDone = data.state === 'done';
  const isPending = data.state === 'pending';
  const pillColor = isActive ? '#fbbf24' : isDone ? '#10b981' : '#64748b';
  const borderColor = isActive
      ? accent
      : isDone
        ? withAlpha('#10b981', 0.45)
        : hovered ? '#3a4d6d' : '#2a3854';
  const boxShadow = isActive
    ? `0 0 0 1px ${accent}, 0 12px 40px ${withAlpha(accent,0.35)}, inset 0 1px 0 rgba(255,255,255,0.08)`
    : hovered
      ? `0 10px 28px rgba(0,0,0,0.5), 0 0 0 1px ${withAlpha(accent, 0.25)}`
      : '0 4px 14px rgba(0,0,0,0.35)';
  const Icon = STAGE_ICONS[data.iconKey];

  return (
    <div
      onClick={() => data.onClick?.()}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        width: 300, borderRadius: 14, padding: 18,
        background: isActive
          ? `linear-gradient(145deg, ${withAlpha(accent,0.08)}, rgba(20,26,40,0.95))`
          : 'rgba(20,26,40,0.92)',
        backdropFilter: 'blur(10px)',
        border: `1px solid ${borderColor}`,
        boxShadow, color: '#e6ebf5', cursor: 'pointer',
        transform: hovered && !isActive ? 'translateY(-2px)' : 'none',
        transition: 'transform 0.18s cubic-bezier(.2,.8,.3,1), border-color 0.2s, box-shadow 0.2s, background 0.3s',
        position: 'relative',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />

      {/* Elapsed timer badge (only when active) */}
      {isActive && data.elapsed && (
        <div style={{
          position: 'absolute', top: -10, right: 16,
          fontSize: 10, fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontWeight: 600,
          padding: '2px 9px', borderRadius: 999,
          background: '#05070d', color: accent,
          border: `1px solid ${withAlpha(accent, 0.5)}`,
          boxShadow: `0 0 12px ${withAlpha(accent, 0.3)}`,
        }}>⏱ {data.elapsed}</div>
      )}

      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', marginBottom: 12 }}>
        <div style={{
          width: 44, height: 44, borderRadius: 10, position: 'relative',
          display:'flex', alignItems:'center', justifyContent:'center',
          background: isActive
            ? `linear-gradient(135deg, ${withAlpha(accent, 0.35)}, ${withAlpha(accent, 0.08)})`
            : withAlpha(accent, isPending ? 0.09 : 0.15),
          border: isActive ? `1px solid ${withAlpha(accent, 0.4)}` : 'none',
          boxShadow: isActive ? `0 0 16px ${withAlpha(accent, 0.4)}, inset 0 1px 0 rgba(255,255,255,0.12)` : 'none',
          opacity: isPending ? 0.7 : 1,
        }}>
          {Icon && (
            <Icon size={20} strokeWidth={isActive ? 2.25 : 1.85}
              color={accent}
              className={isActive && data.stage === 'generating' ? 'animate-spin-slow' : ''}
              style={{
                filter: isActive ? `drop-shadow(0 0 4px ${accent})` : 'none',
                animation: isActive && data.stage !== 'generating'
                  ? 'stagenode-icon-pulse 2.2s ease-in-out infinite' : undefined,
              }} />
          )}
          {/* Pulsing halo for active node */}
          {isActive && (
            <span style={{
              position: 'absolute', inset: -6, borderRadius: 14,
              border: `1px solid ${withAlpha(accent, 0.4)}`,
              animation: 'stagenode-halo 1.8s ease-out infinite',
              pointerEvents: 'none',
            }} />
          )}
        </div>
        <span style={{
          fontSize: 10, textTransform:'uppercase', letterSpacing: '0.2em', fontWeight: 600,
          padding: '5px 10px', borderRadius: 999,
          display:'flex', alignItems:'center', gap: 6,
          background: withAlpha(pillColor, 0.15), color: pillColor,
          border: `1px solid ${withAlpha(pillColor, 0.25)}`,
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: 999, background: 'currentColor',
            animation: isActive ? 'stagenode-dot-blink 1.2s ease-in-out infinite' : undefined,
          }} />
          {pillText}
        </span>
      </div>
      <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: '-0.01em', marginBottom: 4 }}>
        {data.title}
      </div>
      <div style={{ fontSize: 11.5, lineHeight: 1.55, color: isPending ? '#64748b' : '#7f8aa3' }}>{data.desc}</div>
      {data.footer && (
        <div style={{
          marginTop: 11, paddingTop: 9,
          borderTop: `1px dashed ${withAlpha(isActive ? accent : '#ffffff', 0.12)}`,
          fontSize: 11, color: isActive ? accent : '#7f8aa3',
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          fontWeight: isActive ? 600 : 400,
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          {isActive && <span style={{
            width: 5, height: 5, borderRadius: 999, background: accent,
            boxShadow: `0 0 6px ${accent}`,
            animation: 'stagenode-dot-blink 1s ease-in-out infinite',
          }} />}
          {data.footer}
        </div>
      )}
      {isActive && typeof data.progress === 'number' && (
        <div style={{ marginTop: 10, height: 5, background: withAlpha(accent, 0.12), borderRadius: 4, overflow: 'hidden', position: 'relative' }}>
          <div style={{
            width: `${Math.round(data.progress * 100)}%`, height: '100%',
            background: `linear-gradient(90deg, ${accent}, #00e5ff)`,
            boxShadow: `0 0 12px ${accent}`,
            transition: 'width 0.4s',
          }}/>
          {/* Shimmer sweep on the progress bar */}
          <div style={{
            position:'absolute', top: 0, left: 0, height: '100%', width: '30%',
            background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.22), transparent)',
            animation: 'stagenode-shimmer 2s linear infinite',
          }}/>
        </div>
      )}
    </div>
  );
}

const nodeTypes = { stage: StageNode };

function useLotusSocket(token: string, host='localhost', port=8765) {
  const [pipeline, setPipeline] = useState<PipelineState | null>(null);
  const [toast, setToast] = useState<{ kind:'cmd'|'reply'; text: string } | null>(null);
  const [connected, setConnected] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [news, setNews] = useState<NewsPayload | null>(null);
  const [claude, setClaude] = useState<ClaudeSuggestion | null>(null);
  const [analysis, setAnalysis] = useState<NewsAnalysis | null>(null);
  const [postReview, setPostReview] = useState<PostReview | null>(null);
  const [improvement, setImprovement] = useState<PostImprovement | null>(null);
  const [imageGen, setImageGen] = useState<PostImageGen | null>(null);
  const [geminiStatus, setGeminiStatus] = useState<any | null>(null);
  const [geminiTest, setGeminiTest] = useState<any | null>(null);
  // Voice/processing state — "idle" → "listening" → "thinking" → "speaking".
  // "thinking" is set the moment a user_command goes in-flight and cleared
  // when agent_response lands. "speaking" flashes for a few seconds after
  // the reply, roughly the duration of the TTS.
  const [voiceState, setVoiceState] = useState<'idle'|'listening'|'thinking'|'speaking'>('idle');
  const [lastReply, setLastReply] = useState<string>('');
  // Controller alerts — LOTUS (Gemma) emits these when it spots agent
  // trouble or pattern issues. Capped at 20; most-recent first.
  const [alerts, setAlerts] = useState<ControllerAlert[]>([]);
  // Multi-pipeline registry — the mother broadcasts the full list in
  // `pipeline_list` events. `pipeline` (the focused state) comes in as
  // `pipeline_state` and matches `focusPipelineId`.
  const [pipelineList, setPipelineList] = useState<PipelineSummary[]>([]);
  const [focusPipelineId, setFocusPipelineId] = useState<string | null>(null);
  const speakingTimer = useRef<number | undefined>(undefined);
  const wsRef = useRef<WebSocket | null>(null);
  const idCounter = useRef(0);
  const bootIdRef = useRef<string | null>(null);

  const pushActivity = useCallback((kind: ActivityItem['kind'], text: string) => {
    idCounter.current += 1;
    setActivity(prev => [{ id: idCounter.current, ts: Date.now(), kind, text }, ...prev].slice(0, 60));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let retry: number | undefined;
    function connect() {
      if (cancelled) return;
      const ws = new WebSocket(`ws://${host}:${port}`);
      wsRef.current = ws;
      ws.onopen  = () => setConnected(true);
      ws.onclose = () => { setConnected(false); retry = window.setTimeout(connect, 2000); };
      ws.onmessage = (e) => {
        // Broadcast to window for watchdog timers outside this hook.
        window.dispatchEvent(new Event('lotus:ws-activity'));
        let m: any; try { m = JSON.parse(e.data); } catch { return; }
        if (m.type === 'auth_required') ws.send(JSON.stringify({ type:'auth', token }));
        else if (m.type === 'connected') {
          if (bootIdRef.current && m.boot_id && bootIdRef.current !== m.boot_id) {
            window.location.reload();
          }
          bootIdRef.current = m.boot_id || null;
        }
        else if (m.type === 'pipeline_state') {
          setPipeline(prev => {
            if (!prev || prev.stage !== m.stage) pushActivity('stage', `Stage: ${m.stage}`);
            return m;
          });
        }
        else if (m.type === 'pipeline_cancel') { setPipeline(null); pushActivity('info', 'Pipeline cancelled'); }
        else if (m.type === 'pipeline_list') {
          // Multi-pipeline registry broadcast — every active pipeline's
          // summary in one event. Drives the tab bar in the pipeline view.
          const list: PipelineSummary[] = Array.isArray(m.pipelines) ? m.pipelines : [];
          setPipelineList(list);
          if (typeof m.focus_id === 'string') setFocusPipelineId(m.focus_id);
          else if (m.focus_id === null) setFocusPipelineId(null);
        }
        else if (m.type === 'controller_alert') {
          // LOTUS's supervisor is telling us about agent health / a Gemma
          // insight. Prepend; cap list at 20.
          idCounter.current += 1;
          const a: ControllerAlert = {
            id: idCounter.current,
            ts: typeof m.ts === 'number' ? m.ts : Date.now() / 1000,
            level: (m.level === 'warning' || m.level === 'error') ? m.level : 'info',
            agent: String(m.agent || 'LOTUS'),
            message: String(m.message || ''),
          };
          setAlerts(prev => [a, ...prev].slice(0, 20));
          pushActivity('info',
                       `${a.agent}: ${a.message}`.slice(0, 160));
        }
        else if (m.type === 'user_command') {
          setToast({ kind:'cmd', text: m.text });
          window.setTimeout(() => setToast(null), 3500);
          pushActivity('cmd', m.text);
          // Gemma takes over — show thinking animation until the reply lands.
          setVoiceState('thinking');
        } else if (m.type === 'agent_response') {
          setToast({ kind:'reply', text: m.text });
          window.setTimeout(() => setToast(null), 4500);
          pushActivity('reply', m.text);
          setLastReply(m.text || '');
          // Switch thinking → speaking; keep "speaking" on for a duration
          // roughly proportional to the reply length (≈ 70 ms per char,
          // clamped 3-18 s) so the wave animation matches the TTS.
          const ms = Math.min(18000, Math.max(3000, (m.text || '').length * 70));
          setVoiceState('speaking');
          if (speakingTimer.current) clearTimeout(speakingTimer.current);
          speakingTimer.current = window.setTimeout(() => setVoiceState('idle'), ms);
        } else if (m.type === 'voice_listening') {
          setVoiceState(m.active ? 'listening' : 'idle');
        } else if (m.type === 'gemini_status') {
          setGeminiStatus(m);
        } else if (m.type === 'gemini_test_result') {
          setGeminiTest(m);
          pushActivity(m.ok ? 'reply' : 'info',
            `Gemini test ${m.preview}: ${m.ok ? 'OK' : 'FAIL — ' + (m.error||'').slice(0,60)}`);
        } else if (m.type === 'recorder_status') {
          window.dispatchEvent(new CustomEvent('lotus:recorder', {
            detail: { active: !!m.active, elapsed_s: m.elapsed_s||0, file: m.file||null },
          }));
        } else if (m.type === 'recordings_updated') {
          window.dispatchEvent(new CustomEvent('lotus:recordings_updated'));
        } else if (m.type === 'config_updated') {
          // Backend fires this for any config change. The payload may
          // include the specific field that changed (e.g. pipeline);
          // otherwise refetch /api/config (set_projects_root path).
          if (m.config?.pipeline) {
            pushActivity('info', m.message || `Image pipeline → ${m.config.pipeline}`);
          } else {
            fetch('/api/config').then(r => r.json()).then((j) => {
              pushActivity('info', `Projects root → ${j.projects_root}`);
              window.dispatchEvent(new CustomEvent('lotus:config', { detail: j }));
            }).catch(() => {});
          }
        } else if (m.type === 'upload_received') {
          // File landed on the server. The user explicitly asked for
          // immediate confirmation here — silent uploads were confusing.
          const sizeKb = m.size ? `${Math.round(m.size / 1024)} KB` : '?';
          const verdict = m.classification?.verdict
            ? ` — classifier: ${m.classification.verdict}`
            : '';
          pushActivity('info', `Uploaded: ${m.name || 'file'} (${sizeKb})${verdict}`);
        } else if (m.type === 'parsing_started') {
          // The parser has begun extracting prompts. Per-candidate
          // progress is broadcast separately via pipeline_broadcast's
          // processing_label, so we only surface the start marker here.
          pushActivity('info', m.message || `Analyzing prompts${m.chars ? ` (${m.chars} chars)` : ''}…`);
        } else if (m.type === 'parsing_done') {
          // 'reply' style when we found something; 'info' otherwise.
          const kind = (m.count || 0) > 0 ? 'reply' : 'info';
          pushActivity(kind, m.message || `Parsing done — ${m.count || 0} prompts`);
        } else if (m.type === 'post_review') {
          setPostReview({
            status: m.status || 'done',
            index: Number(m.index) || 0,
            total: Number(m.total) || 0,
            id: m.id || '', section: m.section || '', source: m.source || '',
            prompt_text: m.prompt_text || '',
            analysis: m.analysis || '',
            summary: m.summary || '',
            my_take: m.my_take || '',
          });
          if (m.status === 'done')
            pushActivity('reply', `Post review ${m.index+1}/${m.total}: ${(m.my_take||'').slice(0,80)}`);
        } else if (m.type === 'post_review_list') {
          pushActivity('info', `Post-review walk started — ${(m.items||[]).length} posts from ${m.source || 'source'}`);
        } else if (m.type === 'post_image_gen') {
          setImageGen({
            status: m.status || 'done',
            index: Number(m.index) || 0,
            total: Number(m.total) || 0,
            id: m.id || '',
            prompt: m.prompt || '',
            section: m.section || '',
            improved: !!m.improved,
            image_url: m.image_url || '',
            image_path: m.image_path || '',
            folder: m.folder || '',
            filename: m.filename || '',
            parent_folder: m.parent_folder || '',
            expected_url: m.expected_url || '',
            expected_path: m.expected_path || '',
            expected_folder: m.expected_folder || '',
            result: m.result || '',
          });
          if (m.status === 'working') pushActivity('info', `Generating image for post ${(Number(m.index)||0)+1}…`);
          if (m.status === 'done')    pushActivity('reply', `Image saved: ${m.folder || ''}/${m.filename || ''}`);
          if (m.status === 'error')   pushActivity('info', `Image gen failed: ${(m.result||'').slice(0,80)}`);
        } else if (m.type === 'post_improvement') {
          setImprovement({
            status: m.status || 'done',
            index: Number(m.index) || 0,
            total: Number(m.total) || 0,
            id: m.id || '',
            original: m.original || '',
            improved: m.improved || '',
            changes: Array.isArray(m.changes) ? m.changes : [],
            focus: m.focus || '',
          });
          if (m.status === 'done') pushActivity('reply', `Improved post ${m.index+1}: ${(m.improved||'').slice(0,80)}`);
          if (m.status === 'accepted') pushActivity('info', `Improved prompt accepted for post ${m.index+1}`);
          if (m.status === 'rejected') pushActivity('info', `Improved prompt rejected — kept original`);
        } else if (m.type === 'news_headlines') {
          setNews({ topic: m.topic || 'trending', items: m.items || [], error: m.error });
          pushActivity('info', `${(m.items || []).length} ${m.topic} headlines`);
        } else if (m.type === 'claude_suggestion') {
          setClaude({ query: m.query || '', response: m.response || '',
                      score: Number(m.score) || 0, status: m.status || 'done' });
          if (m.status === 'done') pushActivity('reply', `Claude: ${(m.response||'').slice(0,80)}`);
        } else if (m.type === 'news_analysis') {
          setAnalysis({
            title: m.title || '', url: m.url || '',
            analysis: m.analysis || '', status: m.status || 'done',
            has_article: !!m.has_article,
            cursor: typeof m.cursor === 'number' ? m.cursor : undefined,
            total:  typeof m.total  === 'number' ? m.total  : undefined,
            entities:  Array.isArray(m.entities) ? m.entities : undefined,
            source:    m.source    || undefined,
            published: m.published || undefined,
            topic:     m.topic     || undefined,
          });
          if (m.status === 'done') pushActivity('reply', `Gemma analysis: ${(m.title||'').slice(0,80)}`);
        } else if (m.type === 'reload') {
          window.location.reload();
        }
      };
    }
    connect();
    return () => { cancelled = true; if (retry) clearTimeout(retry); wsRef.current?.close(); };
  }, [token, host, port, pushActivity]);

  const send = (obj: any) => {
    if (wsRef.current?.readyState === 1) wsRef.current.send(JSON.stringify(obj));
  };
  const directAction = (action: string, payload: Record<string, any> = {}) =>
    send({ type: 'direct_action', action, ...payload });
  return { pipeline, toast, connected, send, directAction, activity, news, claude, analysis,
           postReview, improvement, imageGen, voiceState, lastReply,
           geminiStatus, geminiTest, setGeminiTest,
           alerts, setAlerts,
           pipelineList, focusPipelineId,
           setClaude, setNews, setAnalysis, setPostReview, setImprovement, setImageGen };
}

function PipelineCanvas({ pipeline, selected, onSelect, onCancel, onEmergencyStop }: {
  pipeline: PipelineState | null;
  selected: StageKey | null;
  onSelect: (k: StageKey) => void;
  onCancel?: () => void;
  onEmergencyStop?: () => void;
}) {
  // Live per-stage elapsed timer. Ticks every second while active stage
  // is running. Resets when stage changes. Shown on the active node.
  const [stageEnteredAt, setStageEnteredAt] = useState<number>(Date.now());
  const [now, setNow] = useState<number>(Date.now());
  useEffect(() => { setStageEnteredAt(Date.now()); }, [pipeline?.stage]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const stageElapsed = useMemo(() => {
    const s = Math.floor((now - stageEnteredAt) / 1000);
    const m = Math.floor(s / 60);
    return m > 0 ? `${m}m ${String(s % 60).padStart(2,'0')}s` : `${s}s`;
  }, [now, stageEnteredAt]);

  // Per-stage position overrides — updated when user drags a node so
  // positions persist through pipeline-state re-renders.
  const [nodePositions, setNodePositions] = useState<Record<string, {x:number; y:number}>>({});
  const onNodeDragStop = useCallback((_evt: any, node: any) => {
    setNodePositions(prev => ({ ...prev, [node.id]: { x: node.position.x, y: node.position.y } }));
  }, []);
  const resetLayout = useCallback(() => setNodePositions({}), []);

  const { nodes, edges } = useMemo(() => {
    const activeIdx = pipeline ? STAGES.findIndex(s => s.key === pipeline.stage) : -1;
    const allDone = pipeline?.stage === 'done';

    const nodes: Node<NodeData>[] = STAGES.map((s, i) => {
      const state: NodeData['state'] = i === activeIdx ? 'active'
                                    : (i < activeIdx || allDone) ? 'done' : 'pending';
      let footer = '';
      let progress: number | undefined;
      if (pipeline) {
        const approved = pipeline.frames.filter(f => f.status === 'approved' || f.final_url).length;
        if (s.key === 'ask_location') {
          footer = pipeline.source_md ? (pipeline.source_md.split('/').pop() || '') : '(awaiting file)';
        } else if (s.key === 'review_prompts') {
          footer = `${pipeline.prompts.length} prompts parsed`;
          if (state === 'active') progress = Math.min(1, pipeline.prompts.length / 10);
        } else if (s.key === 'generating') {
          const gen = pipeline.frames.filter(f => f.temp_url || f.final_url).length;
          const total = pipeline.prompts.length || pipeline.frames.length;
          footer = `${gen}/${total} frames rendered`;
          if (state === 'active' && pipeline.prompts.length) progress = gen / pipeline.prompts.length;
          // Live ETA — (elapsed / done) × remaining; only once we have >=2
          // samples so the first render doesn't produce a wild estimate.
          if (state === 'active' && gen >= 2 && pipeline.created_at && total > gen) {
            const elapsedMs = Date.now() - new Date(pipeline.created_at).getTime();
            if (elapsedMs > 0) {
              const perFrame = elapsedMs / gen;
              const remainingMs = perFrame * (total - gen);
              const m = Math.floor(remainingMs / 60000);
              const s = Math.floor((remainingMs % 60000) / 1000);
              footer += ` · eta ${m > 0 ? `${m}m ` : ''}${String(s).padStart(2,'0')}s`;
            }
          }
        } else if (s.key === 'review_frames') {
          footer = `${pipeline.frames.length} frames · ${approved} approved`;
          if (state === 'active' && pipeline.frames.length) progress = approved / pipeline.frames.length;
        } else if (s.key === 'save') {
          footer = pipeline.save_section ? `→ ${pipeline.save_section}` : '(choose destination)';
        } else if (s.key === 'done') {
          footer = `${pipeline.frames.filter(f => f.final_url).length} files saved`;
        }
      } else {
        footer = '—';
      }
      // Vertical layout by default; user-dragged positions persist in
      // `nodePositions` state (keyed by stage.key) and override the
      // computed coordinates so drag is sticky across re-renders.
      const override = nodePositions[s.key];
      return {
        id: s.key, type: 'stage',
        position: override || { x: 60, y: 40 + i * 220 },
        width: 300, height: 200,
        draggable: true,
        data: { stage:s.key, state, title:s.title, desc:s.desc, iconKey:s.IconKey, color:s.color, footer, progress,
                elapsed: state === 'active' ? stageElapsed : undefined,
                onClick: () => onSelect(s.key) },
        selected: selected === s.key,
      };
    });

    const edges: Edge[] = [];
    for (let i = 0; i < STAGES.length - 1; i++) {
      const done = i < activeIdx || allDone;
      const active = i === activeIdx - 1 && !allDone;
      edges.push({
        id: `${STAGES[i].key}-${STAGES[i+1].key}`,
        source: STAGES[i].key, target: STAGES[i+1].key,
        type: 'smoothstep',
        className: done ? 'done' : active ? 'active' : 'pending',
        markerEnd: { type: 'arrowclosed' as any, width: 18, height: 18 },
      });
    }
    return { nodes, edges };
  }, [pipeline, selected, onSelect, stageElapsed, nodePositions]);

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        proOptions={{ hideAttribution: true }}
        fitView fitViewOptions={{ padding: 0.25, maxZoom: 0.95 }}
        panOnDrag zoomOnScroll nodesDraggable minZoom={0.4} maxZoom={1.5}
        onNodeDragStop={onNodeDragStop}
      >
        <Background variant={BackgroundVariant.Dots} gap={26} size={1.2} color="#1a2238" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor="#141a28" maskColor="rgba(10,15,28,0.8)" />
      </ReactFlow>

      {/* Top-right Pipeline dashboard controls: layout + Cancel + E-Stop.
          Sits inside the Pipeline canvas so it's context-scoped. */}
      <div className="absolute top-3 right-4 z-20 flex items-center gap-2 pointer-events-auto">
        {/* Layout status + reset */}
        <div className="rounded-lg px-2.5 py-1.5 flex items-center gap-1.5"
          style={{
            background: 'rgba(10,15,28,0.7)',
            backdropFilter: 'blur(14px) saturate(160%)',
            border: '1px solid rgba(255,255,255,0.08)',
            boxShadow: '0 4px 14px rgba(0,0,0,0.35)',
          }}>
          <span className="text-[10px] uppercase tracking-widest font-semibold px-1"
            style={{ color: Object.keys(nodePositions).length > 0 ? '#ff6b35' : '#00e5ff' }}>
            {Object.keys(nodePositions).length > 0 ? 'Custom' : 'Vertical'}
          </span>
          {Object.keys(nodePositions).length > 0 && (
            <button onClick={resetLayout}
              title="Restore vertical layout"
              className="px-2 py-1 rounded-md text-[10px] font-semibold uppercase tracking-wider flex items-center gap-1 transition-colors"
              style={{ background: 'rgba(255,107,53,0.12)', color: '#ff6b35', border: '1px solid rgba(255,107,53,0.3)' }}>
              <RefreshCw size={10} /> Reset
            </button>
          )}
          <span style={{ width: 1, height: 14, background: 'rgba(255,255,255,0.08)' }} />
          <span className="text-[10px] text-[#7f8aa3] flex items-center gap-1 px-0.5">
            <Grid3X3 size={10} /> Drag to rearrange
          </span>
        </div>

        {/* Pipeline action buttons — Cancel + Emergency Stop */}
        {pipeline && (
          <div className="rounded-lg px-1.5 py-1 flex items-center gap-1.5"
            style={{
              background: 'rgba(10,15,28,0.7)',
              backdropFilter: 'blur(14px) saturate(160%)',
              border: '1px solid rgba(255,255,255,0.08)',
              boxShadow: '0 4px 14px rgba(0,0,0,0.35)',
            }}>
            {onCancel && <CancelButton onCancel={onCancel} />}
            {onEmergencyStop && <EmergencyStopButton onStop={onEmergencyStop} />}
          </div>
        )}
      </div>
    </div>
  );
}

interface UploadClassification {
  verdict: 'yes' | 'no' | 'uncertain';
  confidence: number;
  reason: string;
  source: 'heuristic' | 'gemma' | 'fallback';
}

function SourceDropzone({ onPath, currentPath }: { onPath: (p: string) => void; currentPath?: string }) {
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>('');
  const [classification, setClassification] = useState<UploadClassification | null>(null);
  const [overridden, setOverridden] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const uploadFile = async (file: File) => {
    setBusy(true); setErr(''); setClassification(null); setOverridden(false);
    try {
      const res = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      });
      const j = await res.json();
      if (j.path) {
        setClassification(j.classification || null);
        // Auto-accept clear YES verdicts; otherwise wait for user to confirm.
        if (j.classification?.verdict === 'yes') {
          onPath(j.path);
        } else {
          // Stash path on the input so the user can confirm-and-load with
          // one click via the override link.
          (window as any)._lotus_pending_upload = j.path;
        }
      } else {
        setErr(j.error || 'Upload failed');
      }
    } catch (e: any) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const acceptPending = () => {
    const p = (window as any)._lotus_pending_upload;
    if (p) {
      setOverridden(true);
      onPath(p);
    }
  };

  const tryNativePicker = async () => {
    const lotus = (window as any).lotus;
    if (lotus?.pickFile) {
      const r = await lotus.pickFile({ filters: [{ name: 'Markdown', extensions: ['md','markdown','txt'] }] });
      // Electron bridge returns { ok, path } or { ok: false }; browser picker won't reach here.
      const p = typeof r === 'string' ? r : (r?.ok ? r.path : null);
      if (p) onPath(p);
      return true;
    }
    return false;
  };

  const handleBrowse = async () => {
    if (await tryNativePicker()) return;
    fileRef.current?.click();
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={async (e) => {
          e.preventDefault(); setDragOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f) await uploadFile(f);
        }}
        onClick={handleBrowse}
        style={{
          border: `2px dashed ${dragOver ? '#ff6b35' : '#232d45'}`,
          borderRadius: 12, padding: 24, textAlign: 'center', cursor: 'pointer',
          background: dragOver ? 'rgba(255,107,53,0.06)' : 'rgba(20,26,40,0.5)',
          transition: 'all 0.2s',
        }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>📄</div>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#e6ebf5', marginBottom: 3 }}>
          {busy ? 'Uploading…' : 'Drop markdown file here'}
        </div>
        <div style={{ fontSize: 11, color: '#7f8aa3' }}>
          or click to browse · .md / .txt · max 50 MB
        </div>
      </div>
      <input ref={fileRef} type="file" accept=".md,.txt,text/*" style={{ display: 'none' }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadFile(f); }} />

      <div style={{ display: 'flex', gap: 6 }}>
        <input
          type="text"
          placeholder="…or paste absolute path: /Users/.../prompts.md"
          defaultValue={currentPath || ''}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              const v = (e.target as HTMLInputElement).value.trim();
              if (v) onPath(v);
            }
          }}
          style={{
            flex: 1, background: '#0a0f1c', border: '1px solid #232d45', borderRadius: 8,
            padding: '8px 10px', color: '#e6ebf5', fontSize: 12, outline: 'none',
          }}
        />
      </div>
      {err && <div style={{ color: '#ef4444', fontSize: 11 }}>{err}</div>}
      {classification && !overridden && classification.verdict !== 'yes' && (
        <div style={{
          padding: '8px 10px', borderRadius: 8, fontSize: 11,
          background: classification.verdict === 'no'
            ? 'rgba(239,68,68,0.08)' : 'rgba(251,191,36,0.08)',
          border: `1px solid ${classification.verdict === 'no'
            ? 'rgba(239,68,68,0.35)' : 'rgba(251,191,36,0.35)'}`,
          color: classification.verdict === 'no' ? '#fca5a5' : '#fcd34d',
        }}>
          <div style={{ fontWeight: 600, marginBottom: 2 }}>
            {classification.verdict === 'no'
              ? "⚠ Doesn't look like a prompts file"
              : '? Classifier uncertain'}
          </div>
          <div style={{ opacity: 0.85, marginBottom: 6 }}>
            {classification.reason} · {classification.source}
          </div>
          <button onClick={acceptPending}
            style={{
              background: 'transparent', border: 'none', padding: 0,
              color: '#fbbf24', fontSize: 11, fontWeight: 600,
              textDecoration: 'underline', cursor: 'pointer',
            }}>
            Treat as prompts file anyway →
          </button>
        </div>
      )}
      {currentPath && (
        <div style={{ fontSize: 11, color: '#10b981', fontFamily: 'JetBrains Mono, monospace', wordBreak: 'break-all' }}>
          ✓ Source: {currentPath}
          {classification?.verdict === 'yes' && (
            <span style={{ color: '#7f8aa3', marginLeft: 8, fontFamily: 'inherit' }}>
              · classified as prompts ({classification.source})
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function PipelineFolderTree({ pipeline, onAction }: {
  pipeline: PipelineState | null;
  onAction: (action: string, payload?: any) => void;
}) {
  const [today, setToday] = useState<{ folder: string; images: any[] }>({ folder: '', images: [] });
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const refresh = useCallback(() => {
    fetch('/api/today').then(r => r.json()).then(setToday).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);
  // Also refresh whenever pipeline.frames length/status changes.
  useEffect(() => { refresh(); }, [pipeline?.frames?.length,
    pipeline?.frames?.filter(f => f.final_url).length, refresh]);

  if (!pipeline) return null;

  // 1) Saved files, grouped by section (from disk).
  const bySection: Record<string, any[]> = {};
  (today.images || []).forEach(img => {
    const k = img.section || '(root)';
    (bySection[k] = bySection[k] || []).push(img);
  });

  // 2) In-memory pipeline frames grouped by status (not yet saved).
  const pending = (pipeline.frames || []).filter(f => !f.final_url);
  const sections = Object.keys(bySection).sort();

  const toggle = (k: string) => setOpen(p => ({ ...p, [k]: !p[k] }));

  const sectionRow = (key: string, label: string, count: number, color: string, children: ReactNode) => (
    <div key={key} style={{
      borderRadius: 8, border: '1px solid #232d45', marginBottom: 6,
      background: 'rgba(10,15,28,0.55)', overflow: 'hidden',
    }}>
      <button onClick={() => toggle(key)}
        style={{
          width: '100%', background: 'transparent', border: 'none',
          padding: '8px 10px', display: 'flex', alignItems: 'center', gap: 8,
          cursor: 'pointer', textAlign: 'left',
        }}>
        <span style={{ color, fontSize: 10, width: 10 }}>{open[key] ? '▾' : '▸'}</span>
        <span style={{ color, fontSize: 13 }}>📁</span>
        <span style={{ color: '#e6ebf5', fontSize: 12, fontWeight: 700,
                       fontFamily: 'JetBrains Mono, monospace' }}>{label}</span>
        <span style={{
          marginLeft: 'auto', fontSize: 10, fontWeight: 700, letterSpacing: '0.15em',
          padding: '2px 8px', borderRadius: 999,
          background: `${color}22`, color, border: `1px solid ${color}55`,
          fontFamily: 'JetBrains Mono, monospace',
        }}>{count} {count === 1 ? 'FILE' : 'FILES'}</span>
      </button>
      {open[key] && (
        <div style={{ padding: '8px 10px 10px', borderTop: '1px dashed rgba(35,45,69,0.6)' }}>
          {children}
        </div>
      )}
    </div>
  );

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 10, color: '#00e5ff', textTransform: 'uppercase',
                    letterSpacing: '0.22em', fontWeight: 700, marginBottom: 6,
                    display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ width: 3, height: 12, background: '#00e5ff', borderRadius: 2 }}/>
        Folders — {today.folder || '(today)'}
      </div>
      {/* Pending frames — in-memory, not yet saved */}
      {pending.length > 0 && sectionRow(
        '__pending__',
        '.pipeline (staging)',
        pending.length,
        '#a78bfa',
        <div className="grid grid-cols-3 gap-1.5">
          {pending.map((f, i) => {
            const url = f.temp_url || '';
            const badgeColor = f.status === 'approved' ? '#10b981'
                             : f.status === 'denied'   ? '#ef4444'
                             : f.status === 'generating' ? '#a78bfa'
                             : '#fbbf24';
            return (
              <div key={f.id} style={{
                border: '1px solid #232d45', borderRadius: 6, padding: 5,
                fontSize: 10, background: '#141a28',
              }}>
                {url ? (
                  <img src={`${url}?cb=${Date.now()}`} alt=""
                    style={{ width: '100%', aspectRatio: '1', objectFit: 'cover',
                             borderRadius: 4, marginBottom: 4 }}/>
                ) : (
                  <div style={{ width: '100%', aspectRatio: '1', borderRadius: 4,
                                background: 'rgba(35,45,69,0.3)', display: 'flex',
                                alignItems: 'center', justifyContent: 'center',
                                marginBottom: 4 }}>
                    <span className="animate-spin" style={{ fontSize: 16, color: '#a78bfa' }}>⚙</span>
                  </div>
                )}
                <div style={{ display: 'flex', justifyContent: 'space-between',
                              alignItems: 'center', marginBottom: 3 }}>
                  <span style={{ fontWeight: 700 }}>F{i+1}</span>
                  <span style={{
                    fontSize: 8.5, padding: '1px 5px', borderRadius: 999,
                    background: `${badgeColor}22`, color: badgeColor, fontWeight: 600,
                    textTransform: 'uppercase', letterSpacing: '0.08em',
                  }}>{(f.status || '').replace('_',' ')}</span>
                </div>
                {f.status === 'pending_review' && (
                  <div style={{ display: 'flex', gap: 3 }}>
                    <button onClick={() => onAction('pipeline_approve_frames', { frame_ids: [f.id] })}
                      style={{ flex: 1, fontSize: 9.5, padding: '3px 0', borderRadius: 3,
                               border: 'none', background: 'rgba(16,185,129,0.18)', color: '#10b981',
                               cursor: 'pointer', fontWeight: 700 }}>✓</button>
                    <button onClick={() => onAction('pipeline_deny_frames', { frame_ids: [f.id] })}
                      style={{ flex: 1, fontSize: 9.5, padding: '3px 0', borderRadius: 3,
                               border: 'none', background: 'rgba(239,68,68,0.18)', color: '#ef4444',
                               cursor: 'pointer', fontWeight: 700 }}>✗</button>
                    <button onClick={() => onAction('pipeline_regen_frame', { id: f.id })}
                      style={{ flex: 1, fontSize: 9.5, padding: '3px 0', borderRadius: 3,
                               border: 'none', background: 'rgba(167,139,250,0.18)', color: '#a78bfa',
                               cursor: 'pointer', fontWeight: 700 }}>↻</button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      {/* Saved sections from disk */}
      {sections.length === 0 && pending.length === 0 && (
        <div className="text-text-dim text-[11px] italic" style={{ padding: 6 }}>
          No files on disk yet. Folders appear here as Gemini saves each frame.
        </div>
      )}
      {sections.map(sec => sectionRow(
        sec,
        `${sec}/`,
        bySection[sec].length,
        '#10b981',
        <div className="grid grid-cols-3 gap-1.5">
          {bySection[sec].map((img: any) => (
            <a key={img.url} href={`${img.url}?cb=${Date.now()}`}
               onClick={(e) => { e.preventDefault();
                 window.dispatchEvent(new CustomEvent('lotus:preview',
                   { detail: { url: img.url, name: img.name, section: sec, folder: today.folder || '' } })); }}
               style={{ display: 'block', border: '1px solid #232d45', borderRadius: 6,
                        overflow: 'hidden', position: 'relative', cursor: 'pointer' }}>
              <img src={`${img.url}?cb=${Date.now()}`} alt={img.name}
                style={{ width: '100%', aspectRatio: '1', objectFit: 'cover' }}/>
              <div style={{
                position: 'absolute', bottom: 0, left: 0, right: 0,
                background: 'linear-gradient(transparent, rgba(0,0,0,0.9))',
                padding: '10px 3px 3px', fontSize: 8.5, color: '#fff',
                fontFamily: 'JetBrains Mono, monospace', textAlign: 'center',
              }}>{img.name}</div>
            </a>
          ))}
        </div>
      ))}
    </div>
  );
}

type FrameFilter = 'all' | 'pending' | 'approved' | 'denied';

function StageDetail({ pipeline, selected, config, onAction, onClose, alerts, onDismissAlert }: {
  pipeline: PipelineState | null;
  selected: StageKey | null;
  config: LotusConfig | null;
  onAction: (action: string, payload?: any) => void;
  onClose: () => void;
  alerts?: ControllerAlert[];
  onDismissAlert?: (id: number) => void;
}) {
  const [frameFilter, setFrameFilter] = useState<FrameFilter>('all');
  const [lightboxFrame, setLightboxFrame] = useState<number | null>(null);
  // Prompt-review state — modal opens when user clicks ✎ on a prompt row.
  const [editingPrompt, setEditingPrompt] = useState<{ id: string; text: string } | null>(null);
  const [promptFilter, setPromptFilter] = useState<'all'|'pending'|'approved'|'rejected'>('all');
  // Collision modal state — populated when /api/frame-upload returns 409.
  const [uploadState, setUploadState] = useState<{
    frameId: string; file: File; defaultName: string; existingName: string;
  } | null>(null);
  // Error log panel open/closed (Phase 2: just a collapsible list).
  const [errorsOpen, setErrorsOpen] = useState(false);
  // Failure-banner expandable resolution-steps panel. Lives in the pipeline
  // header so it's visible across tabs whenever any frame is in 'failed'.
  const [showFailureSteps, setShowFailureSteps] = useState(false);
  // Agents strip — collapsible. Defaults open so users see the mesh
  // working without hunting for it.
  const [agentsOpen, setAgentsOpen] = useState(true);
  // D6: host:port of the child whose detail drawer is open; null = closed.
  const [openChild, setOpenChild] = useState<string | null>(null);
  // Lightbox prompt editor (A18) — when set, user is editing the prompt
  // for the currently-enlarged frame; Regen Edited sends the new text.
  const [lightboxEdit, setLightboxEdit] = useState<string | null>(null);
  // Clear edit state when user navigates to a different frame or closes
  // the lightbox — otherwise edits leak between frames.
  useEffect(() => { setLightboxEdit(null); }, [lightboxFrame]);
  // Per-post checkbox batches (A6) — the checkbox state IS the next batch.
  // Run Batch / Run All Batches sync prompt status to match: ticked →
  // approved, unticked-currently-approved within scope → pending.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // Track the prompt-id signature we last initialised against so we can
  // re-pre-check when the user loads a different MD without clobbering
  // their manual unchecks within the same load.
  const lastPromptSig = useRef<string>('');
  useEffect(() => {
    const sig = (pipeline?.prompts || []).map(p => p.id).sort().join(',');
    if (sig && sig !== lastPromptSig.current) {
      // First render of THIS prompt set — pre-check whatever's already
      // approved so the user sees "these are about to run" without
      // re-ticking 70 boxes.
      lastPromptSig.current = sig;
      setSelectedIds(new Set(
        (pipeline?.prompts || [])
          .filter(p => p.status === 'approved')
          .map(p => p.id)
      ));
    }
  }, [pipeline?.prompts]);

  // Single upload entry point — handles 200 / 409 (collision) / other.
  // The modal calls this a second time with force=true OR newName set
  // once the user chooses how to handle the collision.
  const tryUpload = async (frameId: string, file: File,
                            opts: { force?: boolean; newName?: string } = {}) => {
    const qs = new URLSearchParams({ frame_id: frameId });
    if (opts.force)   qs.set('force', '1');
    if (opts.newName) qs.set('new_name', opts.newName);
    try {
      const r = await fetch(`/api/frame-upload?${qs.toString()}`, {
        method:  'POST',
        body:    file,
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
      });
      const j = await r.json().catch(() => ({} as any));
      if (r.status === 409 && j.collision) {
        setUploadState({
          frameId,
          file,
          defaultName:  j.default_rename || '',
          existingName: j.existing_filename || '',
        });
        return;
      }
      if (!j.ok) {
        alert('Upload failed: ' + (j.message || j.error || `HTTP ${r.status}`));
      }
    } catch (err) {
      alert('Upload error: ' + (err as Error).message);
    }
  };

  // Keyboard shortcuts: Esc closes panel/lightbox; A/D approve/deny the
  // focused frame; ←/→ navigate frames when lightbox open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!pipeline || !selected) return;
      const tgt = e.target as HTMLElement | null;
      const inInput = tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || (tgt as any).isContentEditable);
      if (inInput) return;
      if (e.key === 'Escape') {
        if (lightboxFrame !== null) { setLightboxFrame(null); e.preventDefault(); }
        else onClose();
        return;
      }
      // Lightbox keyboard nav works in any frames-relevant stage; the
      // stage-only gate was stripping shortcuts when user opened the
      // lightbox without first clicking the stage NODE in the canvas.
      const framesActive = (selected === 'review_frames'
        || pipeline?.stage === 'review_frames'
        || pipeline?.stage === 'generating'
        || pipeline?.stage === 'save');
      if (!framesActive) return;
      const frames = pipeline.frames;
      if (lightboxFrame !== null) {
        if (e.key === 'ArrowRight' && lightboxFrame < frames.length - 1)
          setLightboxFrame(lightboxFrame + 1);
        else if (e.key === 'ArrowLeft' && lightboxFrame > 0)
          setLightboxFrame(lightboxFrame - 1);
        else if (e.key.toLowerCase() === 'a') {
          const f = frames[lightboxFrame];
          if (f) onAction('pipeline_approve_frames', { frame_ids: [f.id] });
        } else if (e.key.toLowerCase() === 'd') {
          const f = frames[lightboxFrame];
          if (f) onAction('pipeline_deny_frames', { frame_ids: [f.id] });
        } else if (e.key.toLowerCase() === 'r') {
          const f = frames[lightboxFrame];
          // Backend dispatch is `regen_frame` (with `id`), not
          // `regenerate_frame` — old action name was a silent no-op.
          if (f) onAction('pipeline_regen_frame', { id: f.id });
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pipeline, selected, lightboxFrame, onAction, onClose]);

  if (!pipeline || !selected) return null;
  const meta = STAGES.find(s => s.key === selected)!;
  const accent = COLOR_HEX[meta.color];

  // Filter frames per tab so long lists stay manageable.
  const filteredFrames = (pipeline.frames || []).filter(f => {
    if (frameFilter === 'all') return true;
    if (frameFilter === 'approved') return f.status === 'approved' || !!f.final_url;
    if (frameFilter === 'denied')   return f.status === 'denied';
    if (frameFilter === 'pending')  return f.status === 'pending_review' || f.status === 'generating';
    return true;
  });
  const counts = {
    all:      pipeline.frames?.length ?? 0,
    pending:  pipeline.frames?.filter(f => f.status === 'pending_review' || f.status === 'generating').length ?? 0,
    approved: pipeline.frames?.filter(f => f.status === 'approved' || f.final_url).length ?? 0,
    denied:   pipeline.frames?.filter(f => f.status === 'denied').length ?? 0,
  };
  return (
    <div className="absolute right-4 top-4 bottom-4 w-[420px] bg-surface border border-border rounded-xl
                    overflow-hidden shadow-2xl z-10 flex flex-col">
      <div className="flex items-center gap-3 p-4 border-b border-border-soft">
        <span style={{
          width: 40, height: 40, borderRadius: 10,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 20, background: withAlpha(accent, 0.15), color: accent,
        }}>{(() => {
          const Icon = STAGE_ICONS[meta.IconKey];
          return Icon ? <Icon size={20} /> : null;
        })()}</span>
        <div className="flex-1">
          <div className="font-bold text-text text-[15px]">{meta.title}</div>
          <div className="text-[10px] uppercase tracking-widest text-text-dim">Stage details</div>
        </div>
        <button onClick={onClose} className="text-text-dim hover:text-text text-lg">✕</button>
      </div>
      <div className="overflow-auto flex-1 p-4 space-y-4 text-[13px] text-text-dim leading-relaxed">
        <p>{meta.desc}</p>

        {selected === 'ask_location' && (
          <>
            <SourceDropzone
              currentPath={pipeline.source_md}
              onPath={(p) => onAction('pipeline_set_source', { path: p })}
            />
            {config && (
              <div style={{
                padding: 12, borderRadius: 10, background: '#0a0f1c',
                border: '1px solid #232d45',
              }}>
                <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.2em',
                              color: '#00e5ff', marginBottom: 8, fontWeight: 700 }}>
                  Naming convention
                </div>
                <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#e6ebf5',
                              background: 'rgba(0,229,255,0.05)', padding: '8px 10px', borderRadius: 6,
                              marginBottom: 10, border: '1px solid rgba(0,229,255,0.15)' }}>
                  {config.parent_folder}/<br/>
                  &nbsp;&nbsp;├── Post1/Frame1{config.frame_ext}<br/>
                  &nbsp;&nbsp;├── Reel/Frame1{config.frame_ext}<br/>
                  &nbsp;&nbsp;├── ReelCover{config.frame_ext}<br/>
                  &nbsp;&nbsp;└── Story1{config.frame_ext}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
                  {config.multi_frame_sections.map(s => (
                    <span key={s} style={{
                      fontSize: 10, padding: '3px 8px', borderRadius: 999,
                      background: 'rgba(167,139,250,0.12)', color: '#a78bfa',
                      border: '1px solid rgba(167,139,250,0.25)',
                    }}>{s}/</span>
                  ))}
                  {config.single_item_slots.map(s => (
                    <span key={s} style={{
                      fontSize: 10, padding: '3px 8px', borderRadius: 999,
                      background: 'rgba(255,107,53,0.12)', color: '#ff6b35',
                      border: '1px solid rgba(255,107,53,0.25)',
                    }}>{s}</span>
                  ))}
                </div>
                <div style={{ fontSize: 10.5, color: '#7f8aa3' }}>
                  Parent is auto-named <code style={{ color: '#e6ebf5' }}>{config.brand}&lt;MMDDYYYY&gt;</code>.
                  Frame numbering continues from the max existing file, so runs are resumable.
                </div>
              </div>
            )}
          </>
        )}

        {selected === 'review_prompts' && (() => {
          const prompts = pipeline.prompts || [];
          const counts = {
            approved: prompts.filter(p => p.status === 'approved').length,
            pending:  prompts.filter(p => p.status === 'pending').length,
            rejected: prompts.filter(p => p.status === 'denied' || p.status === 'rejected').length,
          };
          const visible = prompts.filter(p => {
            if (promptFilter === 'all') return true;
            if (promptFilter === 'rejected') return p.status === 'denied' || p.status === 'rejected';
            return p.status === promptFilter;
          });
          const filterBtn = (key: typeof promptFilter, label: string, n: number, color: string) => (
            <button
              key={key}
              onClick={() => setPromptFilter(key)}
              style={{
                padding: '4px 10px', borderRadius: 4, fontSize: 10,
                fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase',
                border: '1px solid',
                borderColor: promptFilter === key ? color : 'rgba(255,255,255,0.08)',
                background: promptFilter === key ? `${color}22` : 'transparent',
                color: promptFilter === key ? color : '#7f8aa3',
                cursor: 'pointer',
              }}>
              {label} <span style={{ opacity: 0.7 }}>{n}</span>
            </button>
          );
          return (
            <div>
              {/* Header: title + counts + filter chips */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
                <div className="text-[11px] uppercase tracking-widest text-amber font-bold">
                  {prompts.length} prompts · review before sending to Gemini
                </div>
                <div style={{ display: 'flex', gap: 4 }}>
                  {filterBtn('all',      'All',      prompts.length, '#e6ebf5')}
                  {filterBtn('pending',  'Pending',  counts.pending,  '#fbbf24')}
                  {filterBtn('approved', 'Approved', counts.approved, '#10b981')}
                  {filterBtn('rejected', 'Rejected', counts.rejected, '#ef4444')}
                </div>
              </div>

              {/* Top batch bar — Select all + Run All Batches */}
              {prompts.length > 0 && (() => {
                const visibleIds = visible.map(p => p.id);
                const selectedVisibleCount = visibleIds.filter(id => selectedIds.has(id)).length;
                const allVisibleSelected = visibleIds.length > 0 && selectedVisibleCount === visibleIds.length;
                // "Runnable" = ticked AND not currently rejected. Approved
                // and pending both count — backend sync semantics will
                // approve pending and keep approved as-is.
                const runnableSelectedIds = visible
                  .filter(p => selectedIds.has(p.id)
                            && p.status !== 'rejected'
                            && p.status !== 'denied')
                  .map(p => p.id);
                const isGenerating = pipeline?.stage === 'generating';
                return (
                  <div style={{ display: 'flex', gap: 8, marginBottom: 10, alignItems: 'center',
                                 flexWrap: 'wrap',
                                 padding: '8px 10px', borderRadius: 6,
                                 background: 'rgba(255,255,255,0.03)',
                                 border: '1px solid rgba(255,255,255,0.06)' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6,
                                     fontSize: 11, color: '#e6ebf5', cursor: 'pointer',
                                     userSelect: 'none' }}>
                      <input type="checkbox"
                        checked={allVisibleSelected}
                        ref={el => { if (el) el.indeterminate = !allVisibleSelected && selectedVisibleCount > 0; }}
                        onChange={() => {
                          setSelectedIds(prev => {
                            const next = new Set(prev);
                            if (allVisibleSelected) visibleIds.forEach(id => next.delete(id));
                            else                    visibleIds.forEach(id => next.add(id));
                            return next;
                          });
                        }}
                        style={{ width: 14, height: 14, cursor: 'pointer' }} />
                      <span style={{ fontWeight: 600 }}>Select all</span>
                      <span style={{ color: '#7f8aa3' }}>
                        ({selectedVisibleCount}/{visibleIds.length})
                      </span>
                    </label>
                    <div style={{ flex: 1 }} />
                    <button onClick={() => onAction('pipeline_approve_all_prompts')}
                      title="Approve every pending prompt without running anything yet"
                      style={{ padding: '6px 12px', borderRadius: 4, fontSize: 10,
                               fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase',
                               border: '1px solid rgba(16,185,129,0.45)',
                               background: 'rgba(16,185,129,0.12)', color: '#10b981',
                               cursor: 'pointer' }}>
                      ✓ Approve all pending
                    </button>
                    <button
                      onClick={() => {
                        if (runnableSelectedIds.length === 0) return;
                        // Master button — no topic scope, full sync across all posts.
                        onAction('pipeline_approve_subset_prompts', { ids: runnableSelectedIds });
                      }}
                      disabled={runnableSelectedIds.length === 0 || isGenerating}
                      title={
                        isGenerating ? 'Generation already running — finish or cancel current batch first'
                        : runnableSelectedIds.length === 0 ? 'Tick at least one prompt (any unrejected)'
                        : `Run ONLY the ${runnableSelectedIds.length} ticked prompts. Any prompt that was approved but is now unticked will revert to pending.`
                      }
                      style={{ padding: '6px 14px', borderRadius: 4, fontSize: 10,
                               fontWeight: 800, letterSpacing: '0.15em', textTransform: 'uppercase',
                               border: 'none',
                               background: (runnableSelectedIds.length > 0 && !isGenerating) ? '#10b981' : 'rgba(127,138,163,0.18)',
                               color:      (runnableSelectedIds.length > 0 && !isGenerating) ? '#062d21' : '#7f8aa3',
                               cursor:     (runnableSelectedIds.length > 0 && !isGenerating) ? 'pointer' : 'not-allowed' }}>
                      ▶▶ Run All Batches ({runnableSelectedIds.length})
                    </button>
                  </div>
                );
              })()}

              {/* Per-post grouped sections */}
              <div style={{ maxHeight: 460, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
                {(() => {
                  // Group visible prompts by topic_number; null/undefined → "Ungrouped"
                  const groups = new Map<number | null, typeof visible>();
                  for (const p of visible) {
                    const k = (p.topic_number ?? null);
                    if (!groups.has(k)) groups.set(k, []);
                    groups.get(k)!.push(p);
                  }
                  const ordered = Array.from(groups.entries()).sort((a, b) => {
                    if (a[0] === null) return 1;
                    if (b[0] === null) return -1;
                    return (a[0] as number) - (b[0] as number);
                  });
                  if (prompts.length === 0)         return <div className="text-text-dim italic">Still parsing…</div>;
                  if (ordered.length === 0)         return <div className="text-text-dim italic">No prompts in this filter.</div>;
                  const isGenerating = pipeline?.stage === 'generating';
                  return ordered.map(([postKey, postPrompts]) => {
                    const postName = postKey != null ? `Post ${postKey}` : 'Ungrouped';
                    const postIds = postPrompts.map(p => p.id);
                    const postSelectedIds = postIds.filter(id => selectedIds.has(id));
                    const allInPostSelected = postIds.length > 0 && postSelectedIds.length === postIds.length;
                    const someInPostSelected = postSelectedIds.length > 0 && !allInPostSelected;
                    // "Runnable" within this post: ticked AND not rejected.
                    // Backend sync semantics will scope to topic_number=postKey
                    // — anything previously approved but now un-ticked in
                    // THIS post reverts to pending.
                    const postRunnableSelectedIds = postPrompts
                      .filter(p => selectedIds.has(p.id)
                                && p.status !== 'rejected'
                                && p.status !== 'denied')
                      .map(p => p.id);
                    return (
                      <div key={String(postKey)}
                        style={{ borderRadius: 8, background: 'rgba(255,255,255,0.02)',
                                  border: '1px solid rgba(255,255,255,0.07)' }}>
                        {/* Post header */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                                       padding: '8px 12px',
                                       borderBottom: '1px solid rgba(255,255,255,0.05)',
                                       background: 'rgba(255,107,53,0.04)' }}>
                          <input type="checkbox"
                            checked={allInPostSelected}
                            ref={el => { if (el) el.indeterminate = someInPostSelected; }}
                            onChange={() => {
                              setSelectedIds(prev => {
                                const next = new Set(prev);
                                if (allInPostSelected) postIds.forEach(id => next.delete(id));
                                else                    postIds.forEach(id => next.add(id));
                                return next;
                              });
                            }}
                            style={{ width: 14, height: 14, cursor: 'pointer' }} />
                          <span style={{ fontSize: 12, fontWeight: 700, color: '#ff8a65',
                                          letterSpacing: '0.05em' }}>
                            {postName}
                          </span>
                          <span style={{ fontSize: 10, color: '#7f8aa3' }}>
                            {postSelectedIds.length} of {postIds.length} selected
                          </span>
                          <div style={{ flex: 1 }} />
                          <button
                            onClick={() => {
                              if (postRunnableSelectedIds.length === 0) return;
                              onAction('pipeline_approve_subset_prompts', {
                                ids: postRunnableSelectedIds,
                                topic_number: postKey,   // scope sync to this post
                              });
                            }}
                            disabled={postRunnableSelectedIds.length === 0 || isGenerating}
                            title={
                              isGenerating ? 'Generation already running'
                              : postRunnableSelectedIds.length === 0 ? 'Tick at least one prompt in this post'
                              : `Run ONLY the ${postRunnableSelectedIds.length} ticked prompt(s) in ${postName}. Other posts and rejected prompts are not touched.`
                            }
                            style={{ padding: '5px 12px', borderRadius: 4, fontSize: 10,
                                     fontWeight: 700, letterSpacing: '0.1em',
                                     textTransform: 'uppercase',
                                     border: 'none',
                                     background: (postRunnableSelectedIds.length > 0 && !isGenerating) ? '#ff6b35' : 'rgba(127,138,163,0.18)',
                                     color:      (postRunnableSelectedIds.length > 0 && !isGenerating) ? '#1a0a05' : '#7f8aa3',
                                     cursor:     (postRunnableSelectedIds.length > 0 && !isGenerating) ? 'pointer' : 'not-allowed' }}>
                            ▶ Run Batch ({postRunnableSelectedIds.length})
                          </button>
                        </div>
                        {/* Post rows */}
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 8 }}>
                          {postPrompts.map((p, i) => {
                            const isApproved = p.status === 'approved';
                            const isRejected = p.status === 'denied' || p.status === 'rejected';
                            const sideColor = isApproved ? '#10b981' : isRejected ? '#ef4444' : '#fbbf24';
                            const titlePath = p.title || '';
                            const slideTag = (p.topic_number != null && p.slide_number != null)
                              ? `T${p.topic_number}·S${String(p.slide_number).padStart(2, '0')}`
                              : (p.slide_number != null ? `S${String(p.slide_number).padStart(2, '0')}` : `#${i+1}`);
                            const checked = selectedIds.has(p.id);
                            return (
                              <div key={p.id}
                                style={{
                                  padding: '10px 12px', borderRadius: 6, fontSize: 12,
                                  background: '#0a0f1c',
                                  border: '1px solid rgba(255,255,255,0.06)',
                                  borderLeft: `3px solid ${sideColor}`,
                                  display: 'flex', gap: 10, alignItems: 'flex-start',
                                }}>
                                <input type="checkbox"
                                  checked={checked}
                                  onChange={() => {
                                    setSelectedIds(prev => {
                                      const next = new Set(prev);
                                      if (next.has(p.id)) next.delete(p.id);
                                      else                next.add(p.id);
                                      return next;
                                    });
                                  }}
                                  style={{ width: 14, height: 14, marginTop: 2, cursor: 'pointer' }} />
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4, flexWrap: 'wrap' }}>
                                    <span style={{ fontSize: 10, fontWeight: 700, color: sideColor,
                                                    letterSpacing: '0.1em', textTransform: 'uppercase' }}>
                                      {slideTag}
                                    </span>
                                    {p.edited && (
                                      <span style={{ fontSize: 9, fontWeight: 700, color: '#a78bfa',
                                                      background: 'rgba(167,139,250,0.12)', padding: '1px 6px',
                                                      borderRadius: 3, letterSpacing: '0.1em' }}>
                                        EDITED
                                      </span>
                                    )}
                                    {titlePath && (
                                      <span style={{ fontSize: 10, color: '#7f8aa3', overflow: 'hidden',
                                                      textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}
                                            title={titlePath}>
                                        {titlePath}
                                      </span>
                                    )}
                                    <span style={{ fontSize: 10, color: '#7f8aa3' }}>
                                      {p.text.length} chars
                                    </span>
                                  </div>
                                  <div style={{ color: '#e6ebf5', lineHeight: 1.45, whiteSpace: 'pre-wrap',
                                                 maxHeight: 80, overflow: 'hidden',
                                                 maskImage: 'linear-gradient(to bottom, #000 60%, transparent 100%)' }}>
                                    {p.text}
                                  </div>
                                </div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                  <button onClick={() => onAction('pipeline_approve_prompt', { id: p.id })}
                                    title="Approve"
                                    style={{ width: 30, height: 26, borderRadius: 4, fontSize: 13,
                                             border: '1px solid',
                                             borderColor: isApproved ? '#10b981' : 'rgba(16,185,129,0.35)',
                                             background:  isApproved ? 'rgba(16,185,129,0.25)' : 'transparent',
                                             color: '#10b981', cursor: 'pointer', fontWeight: 800 }}>
                                    ✓
                                  </button>
                                  <button onClick={() => onAction('pipeline_deny_prompt', { id: p.id })}
                                    title="Reject"
                                    style={{ width: 30, height: 26, borderRadius: 4, fontSize: 13,
                                             border: '1px solid',
                                             borderColor: isRejected ? '#ef4444' : 'rgba(239,68,68,0.35)',
                                             background:  isRejected ? 'rgba(239,68,68,0.25)' : 'transparent',
                                             color: '#ef4444', cursor: 'pointer', fontWeight: 800 }}>
                                    ✗
                                  </button>
                                  <button onClick={() => setEditingPrompt({ id: p.id, text: p.text })}
                                    title="Edit prompt text"
                                    style={{ width: 30, height: 26, borderRadius: 4, fontSize: 13,
                                             border: '1px solid rgba(167,139,250,0.35)',
                                             background: 'transparent', color: '#a78bfa',
                                             cursor: 'pointer', fontWeight: 800 }}>
                                    ✎
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    );
                  });
                })()}
              </div>

              {/* Edit modal */}
              {editingPrompt && (
                <div onClick={() => setEditingPrompt(null)}
                  style={{ position: 'fixed', inset: 0, background: 'rgba(5,8,16,0.78)',
                           zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center',
                           padding: 20 }}>
                  <div onClick={(e) => e.stopPropagation()}
                    style={{ background: '#0a0f1c', border: '1px solid rgba(255,255,255,0.12)',
                              borderRadius: 8, width: 'min(720px, 100%)', maxHeight: '85vh',
                              display: 'flex', flexDirection: 'column' }}>
                    <div style={{ padding: '12px 14px', borderBottom: '1px solid rgba(255,255,255,0.06)',
                                   fontSize: 11, fontWeight: 800, color: '#a78bfa',
                                   letterSpacing: '0.15em', textTransform: 'uppercase' }}>
                      ✎ Edit prompt #{editingPrompt.id}
                    </div>
                    <textarea
                      value={editingPrompt.text}
                      onChange={(e) => setEditingPrompt({ ...editingPrompt, text: e.target.value })}
                      autoFocus
                      style={{ flex: 1, minHeight: 280, padding: 14, background: '#050810',
                                border: 'none', color: '#e6ebf5', fontFamily: 'ui-monospace, monospace',
                                fontSize: 12, lineHeight: 1.5, resize: 'vertical', outline: 'none' }}
                    />
                    <div style={{ display: 'flex', gap: 8, padding: 12,
                                   borderTop: '1px solid rgba(255,255,255,0.06)' }}>
                      <div style={{ flex: 1, fontSize: 10, color: '#7f8aa3', alignSelf: 'center' }}>
                        Saving will revert this prompt to <strong>pending</strong>; approve again to include it.
                      </div>
                      <button onClick={() => setEditingPrompt(null)}
                        style={{ padding: '6px 14px', borderRadius: 4, fontSize: 10,
                                 fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase',
                                 border: '1px solid rgba(255,255,255,0.12)', background: 'transparent',
                                 color: '#7f8aa3', cursor: 'pointer' }}>
                        Cancel
                      </button>
                      <button onClick={() => {
                          onAction('pipeline_edit_prompt', { id: editingPrompt.id, text: editingPrompt.text });
                          setEditingPrompt(null);
                        }}
                        style={{ padding: '6px 14px', borderRadius: 4, fontSize: 10,
                                 fontWeight: 800, letterSpacing: '0.15em', textTransform: 'uppercase',
                                 border: 'none', background: '#a78bfa', color: '#1a1538',
                                 cursor: 'pointer' }}>
                        Save & set pending
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          );
        })()}

        {(selected === 'generating' || selected === 'review_frames') && (
          <div>
            {/* ── Phase 2 multi-agent: controller alerts strip ── */}
            {alerts && alerts.length > 0 && (
              <div style={{ marginBottom: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {alerts.slice(0, 4).map((a) => {
                  const color = a.level === 'error'   ? '#ef4444'
                              : a.level === 'warning' ? '#fbbf24'
                              : '#00e5ff';
                  const icon  = a.level === 'error'   ? '✗'
                              : a.level === 'warning' ? '⚠'
                              : '◇';
                  return (
                    <div key={a.id} style={{
                      background: withAlpha(color, 0.08),
                      border: `1px solid ${withAlpha(color, 0.35)}`,
                      borderRadius: 6, padding: '6px 10px',
                      display: 'flex', alignItems: 'center', gap: 8, fontSize: 11,
                    }}>
                      <span style={{ color, fontWeight: 800, fontSize: 13 }}>{icon}</span>
                      <div style={{ flex: 1, lineHeight: 1.4 }}>
                        <span style={{ color, fontWeight: 700, letterSpacing: '0.08em',
                                        textTransform: 'uppercase', fontSize: 9,
                                        marginRight: 6 }}>
                          {a.agent}
                        </span>
                        <span style={{ color: '#e6ebf5' }}>{a.message}</span>
                        <span style={{ marginLeft: 8, color: '#7f8aa3', fontSize: 9 }}>
                          {new Date(a.ts * 1000).toLocaleTimeString()}
                        </span>
                      </div>
                      <button onClick={() => onDismissAlert?.(a.id)}
                        title="Dismiss"
                        style={{ background: 'transparent', border: 'none',
                                 color: '#7f8aa3', cursor: 'pointer', fontSize: 14,
                                 padding: '0 4px' }}>
                        ×
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
            {/* ── end controller alerts ── */}
            {/* ── Failure banner — appears whenever ANY frame is in 'failed'
                  status, regardless of pipeline.paused. Non-blocking: the
                  render loop continues underneath; this banner is purely
                  informational + actionable. Shows the first 5 failed frame
                  IDs and expandable resolution steps. Auto-disappears once
                  the failed count reaches 0 (user retries / uploads / skips
                  via the per-frame buttons in the review_frames grid). ── */}
            {(() => {
              const failedFrames = pipeline.frames.filter(f => f.status === 'failed');
              if (failedFrames.length === 0) return null;
              const preview = failedFrames.slice(0, 5).map(f =>
                `${f.section || '?'}/${f.id}`).join(', ');
              const more = failedFrames.length > 5
                ? `, +${failedFrames.length - 5} more` : '';
              return (
                <div style={{
                  marginBottom: 10, padding: '10px 12px', borderRadius: 8,
                  background: 'rgba(239,68,68,0.10)',
                  border: '1px solid rgba(239,68,68,0.40)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start',
                                justifyContent: 'space-between', gap: 12 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 11, fontWeight: 800, color: '#fca5a5',
                                    textTransform: 'uppercase', letterSpacing: '0.15em' }}>
                        ⚠ {failedFrames.length} frame{failedFrames.length === 1 ? '' : 's'} need attention
                      </div>
                      <div style={{ fontSize: 11, color: '#e6ebf5', marginTop: 4,
                                    fontFamily: 'monospace', wordBreak: 'break-word' }}>
                        {preview}{more}
                      </div>
                    </div>
                    <button onClick={() => setShowFailureSteps(v => !v)}
                      style={{ padding: '6px 10px', borderRadius: 6, fontSize: 10,
                               fontWeight: 700, letterSpacing: '0.10em',
                               textTransform: 'uppercase', cursor: 'pointer',
                               background: 'rgba(239,68,68,0.15)', color: '#fca5a5',
                               border: '1px solid rgba(239,68,68,0.40)',
                               whiteSpace: 'nowrap' }}>
                      {showFailureSteps ? 'Hide steps' : 'How to fix'}
                    </button>
                  </div>
                  {showFailureSteps && (
                    <div style={{ marginTop: 10, paddingTop: 10,
                                  borderTop: '1px dashed rgba(239,68,68,0.30)',
                                  fontSize: 11, color: '#e6ebf5', lineHeight: 1.55 }}>
                      <div style={{ fontWeight: 700, color: '#fca5a5', marginBottom: 6 }}>
                        Per failed frame in the Review Frames grid below, choose one:
                      </div>
                      <div style={{ paddingLeft: 4 }}>
                        <div><strong>↻ Retry</strong> — re-runs the prompt through Gemini.
                          Best for transient failures (network blip, content-policy false positive).
                          Gives up after 3 attempts.</div>
                        <div style={{ marginTop: 4 }}>
                          <strong>↑ Upload</strong> — generate the image manually
                          (e.g. open Gemini in a normal tab, paste the prompt,
                          download with the hover-toolbar Download icon), then click Upload
                          and select the file.</div>
                        <div style={{ marginTop: 4 }}>
                          <strong>⊝ Skip</strong> — mark this slot as intentionally empty.
                          Use when the prompt itself is unrecoverable (e.g. content
                          consistently rejected).</div>
                        <div style={{ marginTop: 4 }}>
                          <strong>⧉ Copy</strong> — copy the prompt text to clipboard
                          (handy if you're going to upload manually).</div>
                      </div>
                      <div style={{ marginTop: 8, fontStyle: 'italic',
                                    color: '#9ca3af' }}>
                        Pipeline keeps generating remaining frames in the
                        background — no need to resolve everything before
                        continuing.
                      </div>
                    </div>
                  )}
                </div>
              );
            })()}
            {/* ── Phase 2: pause banner + report strip + error log ── */}
            {pipeline.paused && (
              <div style={{
                marginBottom: 10, padding: '10px 12px', borderRadius: 8,
                background: 'rgba(251,191,36,0.12)',
                border: '1px solid rgba(251,191,36,0.45)',
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                gap: 10,
              }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 11, fontWeight: 800, color: '#fbbf24',
                                textTransform: 'uppercase', letterSpacing: '0.15em' }}>
                    ⏸ Paused
                  </div>
                  <div style={{ fontSize: 11, color: '#e6ebf5', marginTop: 3 }}>
                    {pipeline.pause_reason || 'Resolve failed frames, then resume.'}
                  </div>
                </div>
                <button onClick={() => onAction('pipeline_resume')}
                  style={{ padding: '8px 16px', borderRadius: 6, fontSize: 11,
                           fontWeight: 800, letterSpacing: '0.15em',
                           textTransform: 'uppercase', cursor: 'pointer',
                           background: '#10b981', color: '#062d21', border: 'none' }}>
                  ▶ Resume
                </button>
              </div>
            )}
            {(() => {
              const by = (k: string) => pipeline.frames.filter(f => f.source === k).length;
              const nLotus    = by('lotus');
              const nManual   = by('manual');
              const nSkipped  = by('skipped');
              const nFailed   = pipeline.frames.filter(f => f.status === 'failed').length;
              const nOpen     = pipeline.frames.filter(f => f.status === 'generating').length;
              const started   = typeof pipeline.created_at === 'number'
                ? pipeline.created_at * 1000
                : (pipeline.created_at ? Date.parse(String(pipeline.created_at)) : null);
              const elapsedS  = started ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : null;
              const elapsedTxt = elapsedS === null ? '—'
                : elapsedS < 60 ? `${elapsedS}s`
                : elapsedS < 3600 ? `${Math.floor(elapsedS/60)}m ${elapsedS%60}s`
                : `${Math.floor(elapsedS/3600)}h ${Math.floor((elapsedS%3600)/60)}m`;
              const cell = (label: string, value: number | string, color: string) => (
                <div key={label} style={{
                  flex: 1, minWidth: 70, padding: '6px 8px',
                  background: withAlpha(color, 0.08),
                  border: `1px solid ${withAlpha(color, 0.3)}`,
                  borderRadius: 6, textAlign: 'center',
                }}>
                  <div style={{ fontSize: 16, fontWeight: 800, color }}>{value}</div>
                  <div style={{ fontSize: 9, color: '#7f8aa3',
                                textTransform: 'uppercase', letterSpacing: '0.12em',
                                marginTop: 2 }}>
                    {label}
                  </div>
                </div>
              );
              return (
                <div style={{ display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap',
                              alignItems: 'stretch' }}>
                  {cell('LOTUS',   nLotus,   '#a78bfa')}
                  {cell('Manual',  nManual,  '#00e5ff')}
                  {cell('Skipped', nSkipped, '#7f8aa3')}
                  {cell('Failed',  nFailed,  '#ef4444')}
                  {cell('Open',    nOpen,    '#fbbf24')}
                  {cell('Elapsed', elapsedTxt, '#e6ebf5')}
                  {/* Archive Run button — asks ArchiverAgent to freeze the
                      current pipeline to disk as a JSON report with a
                      Gemma-written summary. */}
                  <button onClick={() => onAction('pipeline_archive')}
                    title="Hand the current pipeline state to ArchiverAgent — produces a timestamped JSON report at ~/LotusAgent/reports/ with a Gemma-written summary you can share with a client."
                    style={{
                      flex: 1, minWidth: 70, padding: '6px 8px',
                      background: 'rgba(167,139,250,0.12)',
                      border: '1px solid rgba(167,139,250,0.4)',
                      borderRadius: 6, cursor: 'pointer', color: '#a78bfa',
                      fontFamily: 'inherit',
                      display: 'flex', flexDirection: 'column', alignItems: 'center',
                      justifyContent: 'center',
                    }}>
                    <div style={{ fontSize: 16, fontWeight: 800 }}>📦</div>
                    <div style={{ fontSize: 9, textTransform: 'uppercase',
                                  letterSpacing: '0.12em', marginTop: 2,
                                  fontWeight: 700 }}>
                      Archive
                    </div>
                  </button>
                </div>
              );
            })()}
            {pipeline.errors && pipeline.errors.length > 0 && (
              <div style={{ marginBottom: 10, border: '1px solid #232d45',
                            borderRadius: 6, overflow: 'hidden' }}>
                <div style={{ display: 'flex', background: 'rgba(239,68,68,0.08)' }}>
                  <button onClick={() => setErrorsOpen(v => !v)}
                    style={{ flex: 1, padding: '6px 10px', background: 'transparent',
                             border: 'none', color: '#ef4444', fontSize: 10, fontWeight: 700,
                             textTransform: 'uppercase', letterSpacing: '0.15em',
                             display: 'flex', justifyContent: 'space-between',
                             cursor: 'pointer', alignItems: 'center' }}>
                    <span>⚠ Error log · {pipeline.errors.length}</span>
                    <span>{errorsOpen ? '▲' : '▼'}</span>
                  </button>
                  <button onClick={(e) => {
                    e.stopPropagation();
                    // Build a customer-shareable JSON of this pipeline's
                    // full state + a computed summary. No backend round-trip —
                    // the dashboard already has the authoritative state.
                    const summary = {
                      by_lotus:  pipeline.frames.filter(f => f.source === 'lotus').length,
                      manual:    pipeline.frames.filter(f => f.source === 'manual').length,
                      skipped:   pipeline.frames.filter(f => f.source === 'skipped').length,
                      failed:    pipeline.frames.filter(f => f.status === 'failed').length,
                      generating:pipeline.frames.filter(f => f.status === 'generating').length,
                      total:     pipeline.frames.length,
                    };
                    const payload = {
                      exported_at: new Date().toISOString(),
                      pipeline_id: pipeline.id,
                      pipeline_name: pipeline.name,
                      source_md: pipeline.source_md,
                      stage: pipeline.stage,
                      paused: pipeline.paused || false,
                      pause_reason: pipeline.pause_reason || null,
                      created_at: pipeline.created_at || null,
                      summary,
                      prompts: pipeline.prompts,
                      frames: pipeline.frames,
                      errors: pipeline.errors,
                    };
                    const blob = new Blob([JSON.stringify(payload, null, 2)],
                      { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    const safe = (pipeline.name || pipeline.id || 'pipeline')
                      .replace(/[^a-z0-9_-]+/gi, '_');
                    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
                    a.href = url;
                    a.download = `lotus-${safe}-${ts}.json`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                  }}
                    title="Download full pipeline state as JSON — prompts, frames, errors, timestamps, counts"
                    style={{ padding: '6px 12px', background: 'transparent',
                             borderLeft: '1px solid rgba(239,68,68,0.25)',
                             borderTop: 'none', borderBottom: 'none', borderRight: 'none',
                             color: '#e6ebf5', fontSize: 10, fontWeight: 700,
                             textTransform: 'uppercase', letterSpacing: '0.15em',
                             cursor: 'pointer' }}>
                    ⇩ Export
                  </button>
                </div>
                {errorsOpen && (
                  <div style={{ maxHeight: 180, overflowY: 'auto', background: '#0a0f1c' }}>
                    {pipeline.errors.slice().reverse().map((e, i) => (
                      <div key={i} style={{
                        padding: '5px 10px',
                        borderTop: i === 0 ? 'none' : '1px solid #182238',
                        fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                        color: '#e6ebf5', lineHeight: 1.45,
                      }}>
                        <span style={{ color: '#7f8aa3' }}>
                          {new Date(e.ts * 1000).toLocaleTimeString()}
                        </span>
                        {' '}
                        <span style={{ color: '#a78bfa' }}>{e.section || '—'}</span>
                        {' · '}
                        <span style={{ color: '#00e5ff' }}>{e.frame_id || '—'}</span>
                        {' · '}
                        <span style={{ color: '#7f8aa3' }}>{e.stage}</span>
                        <div style={{ color: '#ef4444', marginTop: 1 }}>{e.message}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {/* ── end Phase 2 bar ── */}
            {/* ── Phase 3-agents: mesh status strip (collapsible) ── */}
            {pipeline.agents && pipeline.agents.length > 0 && (
              <div style={{ marginBottom: 10, border: '1px solid #232d45',
                            borderRadius: 6, overflow: 'hidden' }}>
                <button onClick={() => setAgentsOpen(v => !v)}
                  style={{ width: '100%', padding: '6px 10px',
                           background: 'rgba(167,139,250,0.08)',
                           border: 'none', color: '#a78bfa', fontSize: 10, fontWeight: 700,
                           textTransform: 'uppercase', letterSpacing: '0.15em',
                           display: 'flex', justifyContent: 'space-between',
                           cursor: 'pointer', alignItems: 'center' }}>
                  <span>◇ Agent Mesh · {pipeline.agents.length}</span>
                  <span>{agentsOpen ? '▲' : '▼'}</span>
                </button>
                {agentsOpen && (() => {
                  // D5: group agents by host so the user sees which
                  // machine hosts what. "Local" sits first; each remote
                  // child gets its own group labeled by host:port.
                  const groups: Record<string, AgentHealth[]> = {};
                  for (const a of pipeline.agents) {
                    const k = a.remote ? (a.host || 'remote') : 'Local';
                    (groups[k] ||= []).push(a);
                  }
                  const groupKeys = Object.keys(groups)
                    .sort((a, b) => a === 'Local' ? -1 : b === 'Local' ? 1 : a.localeCompare(b));
                  return (
                  <div style={{ padding: '6px 8px', background: '#0a0f1c',
                                display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {groupKeys.map((hostKey) => (
                  <div key={hostKey} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <div
                      onClick={() => { if (hostKey !== 'Local') setOpenChild(hostKey); }}
                      style={{ fontSize: 9, color: '#7f8aa3',
                               letterSpacing: '0.15em', textTransform: 'uppercase',
                               fontWeight: 700, marginBottom: 2,
                               cursor: hostKey === 'Local' ? 'default' : 'pointer',
                               display: 'flex', alignItems: 'center', gap: 8 }}
                      title={hostKey === 'Local'
                        ? 'Agents running in the mother process'
                        : `Click for details + controls for ${hostKey}`}>
                      <span>
                        {hostKey === 'Local'
                          ? '▣ LOCAL (this machine)'
                          : `🛰 CHILD @ ${hostKey}`}
                      </span>
                      <span style={{ color: '#4e5872', fontWeight: 500 }}>
                        · {groups[hostKey].length} agent{groups[hostKey].length > 1 ? 's' : ''}
                      </span>
                      {hostKey !== 'Local' && (
                        <span style={{ marginLeft: 'auto', color: '#00e5ff', fontSize: 9 }}>
                          details ›
                        </span>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {groups[hostKey].map((a, idx) => {
                      const iconMap: Record<string,string> = {
                        ParserAgent:   '📄',
                        RendererAgent: '🎨',
                        ReviewAgent:   '🔍',
                        WriterAgent:   '✍',
                        ArchiverAgent: '🗂',
                      };
                      const icon = iconMap[a.name] || '◇';
                      const rateTxt = a.success_rate === null
                        ? '—'
                        : `${Math.round((a.success_rate || 0) * 100)}%`;
                      const isBusy = a.busy > 0;
                      const hasRecentFails = (a.recent_failures || 0) > 0;
                      const stateColor = !a.alive      ? '#ef4444'
                                        : hasRecentFails ? '#fbbf24'  // circuit-breaker warning
                                        : isBusy      ? '#fbbf24'
                                        : a.fail_count > a.success_count ? '#ef4444'
                                        : '#10b981';
                      const tooltip = [
                        a.remote ? `REMOTE @ ${a.host}` : 'LOCAL',
                        a.last_error ? `Last error: ${a.last_error}` : null,
                        hasRecentFails ? `Circuit breaker: ${a.recent_failures} recent failures — deprioritised` : null,
                      ].filter(Boolean).join(' · ');
                      return (
                        <div key={`${a.name}-${idx}`} title={tooltip}
                          style={{
                            flex: '1 1 150px', minWidth: 150,
                            background: '#05080f',
                            border: `1px solid ${withAlpha(stateColor, 0.35)}`,
                            borderRadius: 6, padding: '6px 8px',
                            fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
                            position: 'relative',
                          }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between',
                                         alignItems: 'center', gap: 6, marginBottom: 3 }}>
                            <span style={{ color: '#e6ebf5', fontWeight: 700 }}>
                              <span style={{ marginRight: 5 }}>{icon}</span>{a.name}
                              {a.remote && (
                                <span style={{ marginLeft: 4, color: '#00e5ff',
                                               fontSize: 9 }}
                                      title={`Remote agent at ${a.host}`}>🛰</span>
                              )}
                            </span>
                            <span style={{
                              fontSize: 8, padding: '1px 5px', borderRadius: 999,
                              background: withAlpha(stateColor, 0.18),
                              color: stateColor, letterSpacing: '0.12em', fontWeight: 800,
                              textTransform: 'uppercase',
                            }}>
                              {isBusy ? `${a.busy} BUSY` : a.alive ? 'IDLE' : 'DOWN'}
                            </span>
                          </div>
                          {a.remote && a.host && (
                            <div style={{ color: '#00e5ff', fontSize: 9, marginBottom: 2,
                                          letterSpacing: '0.05em' }}>
                              @ {a.host}
                            </div>
                          )}
                          <div style={{ display: 'flex', justifyContent: 'space-between',
                                         color: '#7f8aa3' }}>
                            <span>
                              <span style={{ color: '#10b981' }}>✓{a.success_count}</span>
                              {' '}
                              <span style={{ color: '#ef4444' }}>✗{a.fail_count}</span>
                              {hasRecentFails && (
                                <span style={{ color: '#fbbf24', marginLeft: 4 }}
                                      title="Circuit breaker active — this provider is temporarily deprioritised">
                                  ⚡{a.recent_failures}
                                </span>
                              )}
                            </span>
                            <span style={{ color: '#e6ebf5' }}>{rateTxt}</span>
                          </div>
                          {a.last_error && (
                            <div style={{ color: '#ef4444', marginTop: 3,
                                          fontSize: 9, lineHeight: 1.3,
                                          overflow: 'hidden',
                                          textOverflow: 'ellipsis',
                                          whiteSpace: 'nowrap' }}>
                              {a.last_error.length > 42
                                ? a.last_error.slice(0, 42) + '…'
                                : a.last_error}
                            </div>
                          )}
                        </div>
                      );
                    })}
                    </div>
                  </div>
                  ))}
                  </div>
                  );
                })()}
              </div>
            )}
            {/* ── end Phase 3-agents strip ── */}
            <div className="text-[11px] uppercase tracking-widest mb-2 text-violet font-bold">
              {pipeline.frames.length} frames
              {' · '}
              <span className="text-green">{pipeline.frames.filter(f => f.status === 'approved' || f.final_url).length} approved</span>
            </div>
            {/* Live folder tree: groups in-flight pipeline frames by section
                folder with counts, expandable thumbnails, approve / reject
                per frame. Uses the /api/today endpoint as ground truth for
                saved frames; merges in in-memory pipeline.frames too. */}
            <PipelineFolderTree pipeline={pipeline} onAction={onAction} />
            <div className="flex items-center justify-between mt-3 mb-2">
              <div className="text-[10px] uppercase tracking-widest text-text-dim font-bold">
                Current batch
              </div>
              {selected === 'review_frames' && (
                <div className="flex items-center gap-1 bg-[#05070d]/60 rounded-lg p-0.5 border border-[#1e2638]">
                  {(['all','pending','approved','denied'] as FrameFilter[]).map(key => {
                    const active = frameFilter === key;
                    const label = key[0].toUpperCase() + key.slice(1);
                    const n = counts[key];
                    const tone = key === 'approved' ? '#10b981'
                               : key === 'denied' ? '#ef4444'
                               : key === 'pending' ? '#fbbf24'
                               : '#00e5ff';
                    return (
                      <button key={key} onClick={() => setFrameFilter(key)}
                        className="px-2 py-1 rounded-md text-[10px] font-semibold uppercase tracking-wider transition-colors flex items-center gap-1.5"
                        style={{
                          background: active ? withAlpha(tone, 0.15) : 'transparent',
                          color: active ? tone : '#7f8aa3',
                          border: `1px solid ${active ? withAlpha(tone, 0.35) : 'transparent'}`,
                        }}>
                        {label}
                        <span style={{
                          fontSize: 9, padding: '1px 5px', borderRadius: 999,
                          background: active ? withAlpha(tone, 0.2) : 'rgba(255,255,255,0.06)',
                          color: active ? tone : '#7f8aa3',
                          minWidth: 18, textAlign: 'center',
                        }}>{n}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {filteredFrames.map((f) => {
                // Use the frame's ORIGINAL index in pipeline.frames so the
                // label matches the real frame number and the lightbox can
                // step through the full set via arrow keys.
                const origIdx = pipeline.frames.findIndex(x => x.id === f.id);
                const thumb = f.temp_url || f.final_url;
                const url = thumb ? `${thumb}` : null;
                const badgeColor = f.status === 'approved' || (f.final_url && f.status === 'pending_review') ? '#10b981'
                                 : f.status === 'denied' ? '#ef4444'
                                 : f.status === 'failed'  ? '#ef4444'
                                 : f.status === 'skipped' ? '#7f8aa3'
                                 : f.status === 'generating' ? '#a78bfa'
                                 : '#fbbf24';
                // Origin badge — shows who produced this frame. Drives
                // the report tally (LOTUS vs manual vs skipped).
                const srcBadge = f.source === 'manual' ? { label: 'MANUAL', color: '#00e5ff' }
                               : f.source === 'lotus'  ? { label: 'LOTUS',  color: '#a78bfa' }
                               : f.source === 'skipped'? { label: 'SKIPPED',color: '#7f8aa3' }
                               : null;
                return (
                  <div key={f.id}
                    onClick={() => { if (url) setLightboxFrame(origIdx); }}
                    style={{
                      background: '#0a0f1c', border: `1px solid ${withAlpha(badgeColor, 0.22)}`, borderRadius: 8,
                      padding: 6, fontSize: 11, cursor: url ? 'pointer' : 'default',
                      transition: 'transform 0.15s, box-shadow 0.2s, border-color 0.2s',
                    }}
                    onMouseEnter={(e) => { if (url) {
                      (e.currentTarget as HTMLElement).style.transform = 'translateY(-2px)';
                      (e.currentTarget as HTMLElement).style.boxShadow = `0 8px 20px ${withAlpha(badgeColor, 0.25)}`;
                      (e.currentTarget as HTMLElement).style.borderColor = withAlpha(badgeColor, 0.55);
                    }}}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLElement).style.transform = 'none';
                      (e.currentTarget as HTMLElement).style.boxShadow = 'none';
                      (e.currentTarget as HTMLElement).style.borderColor = withAlpha(badgeColor, 0.22);
                    }}>
                    {url ? (
                      <img src={url} alt="" style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 4 }} />
                    ) : (
                      <div style={{
                        width: '100%', aspectRatio: '1', borderRadius: 4,
                        background: 'rgba(35,45,69,0.3)',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        {f.status === 'generating' ? (
                          <Sparkles size={20} className="animate-spin-slow" color="#a78bfa" />
                        ) : <span style={{ color: '#4e5872' }}>·</span>}
                      </div>
                    )}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 6, gap: 4 }}>
                      <span style={{ fontWeight: 600 }}
                            title={f.error || ''}>
                        Frame {origIdx + 1}
                      </span>
                      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                        {(f.retry_count ?? 0) > 0 && (
                          <span title={`Retried ${f.retry_count} of 3 by ReviewAgent`}
                            style={{
                              fontSize: 8, padding: '2px 5px', borderRadius: 999,
                              background: 'rgba(251,191,36,0.15)', color: '#fbbf24',
                              letterSpacing: '0.12em', fontWeight: 700,
                            }}>↻{f.retry_count}</span>
                        )}
                        {srcBadge && (
                          <span style={{
                            fontSize: 8, padding: '2px 5px', borderRadius: 999,
                            background: withAlpha(srcBadge.color, 0.15), color: srcBadge.color,
                            letterSpacing: '0.12em', fontWeight: 700,
                          }}>{srcBadge.label}</span>
                        )}
                        <span style={{
                          fontSize: 9, padding: '2px 6px', borderRadius: 999,
                          background: withAlpha(badgeColor, 0.15), color: badgeColor,
                          textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 600,
                        }}>{f.status}</span>
                      </div>
                    </div>
                    {f.status === 'failed' && f.error && (
                      <div style={{ fontSize: 9, color: '#ef4444', marginTop: 4,
                                    fontStyle: 'italic', lineHeight: 1.3,
                                    wordBreak: 'break-word' }}
                           title={f.error}>
                        {f.error.length > 72 ? f.error.slice(0, 72) + '…' : f.error}
                      </div>
                    )}
                    {selected === 'review_frames' && f.status === 'pending_review' && (
                      <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                        <button onClick={(e) => { e.stopPropagation(); onAction('pipeline_approve_frames', { frame_ids: [f.id] }); }}
                          style={{ flex: 1, fontSize: 10, padding: '5px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(16,185,129,0.18)', color: '#10b981',
                                   border: '1px solid rgba(16,185,129,0.3)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>✓ Approve</button>
                        <button onClick={(e) => { e.stopPropagation(); onAction('pipeline_deny_frames', { frame_ids: [f.id] }); }}
                          style={{ flex: 1, fontSize: 10, padding: '5px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(239,68,68,0.18)', color: '#ef4444',
                                   border: '1px solid rgba(239,68,68,0.3)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>✗ Deny</button>
                      </div>
                    )}
                    {f.status === 'failed' && (
                      <div style={{ display: 'flex', gap: 3, marginTop: 6, flexWrap: 'wrap' }}>
                        <button onClick={(e) => {
                          e.stopPropagation();
                          try { navigator.clipboard.writeText(f.prompt_text || ''); }
                          catch { /* older browser fallback */ }
                        }}
                          title="Copy prompt text — paste into Gemini to regenerate manually"
                          style={{ flex: '1 1 22%', fontSize: 9, padding: '4px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(0,229,255,0.15)', color: '#00e5ff',
                                   border: '1px solid rgba(0,229,255,0.3)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>⧉ Copy</button>
                        <button onClick={(e) => {
                          e.stopPropagation();
                          const input = document.createElement('input');
                          input.type = 'file';
                          input.accept = 'image/*';
                          input.onchange = async (evt: Event) => {
                            const file = (evt.target as HTMLInputElement).files?.[0];
                            if (!file) return;
                            await tryUpload(f.id, file);
                          };
                          input.click();
                        }}
                          title={`Upload — saves to ${f.section}/Frame${(f.slide_idx ?? 0) + 1}.png; if the file already exists you'll get a prompt to Overwrite or Save As`}
                          style={{ flex: '1 1 22%', fontSize: 9, padding: '4px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(167,139,250,0.18)', color: '#a78bfa',
                                   border: '1px solid rgba(167,139,250,0.35)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>↑ Upload</button>
                        <button onClick={(e) => {
                          e.stopPropagation();
                          onAction('pipeline_frame_retry', { id: f.id });
                        }}
                          title="Retry via LOTUS bot — re-runs the prompt in Gemini, saves on success"
                          style={{ flex: '1 1 22%', fontSize: 9, padding: '4px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(16,185,129,0.18)', color: '#10b981',
                                   border: '1px solid rgba(16,185,129,0.3)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>↻ Retry</button>
                        <button onClick={(e) => {
                          e.stopPropagation();
                          onAction('pipeline_frame_skip', { id: f.id });
                        }}
                          title="Mark this slot as intentionally empty — counted separately from failures"
                          style={{ flex: '1 1 22%', fontSize: 9, padding: '4px 0', borderRadius: 4, fontWeight: 700,
                                   background: 'rgba(127,138,163,0.18)', color: '#7f8aa3',
                                   border: '1px solid rgba(127,138,163,0.3)', cursor: 'pointer',
                                   textTransform: 'uppercase', letterSpacing: '0.08em' }}>⊝ Skip</button>
                      </div>
                    )}
                  </div>
                );
              })}
              {pipeline.frames.length === 0 && (
                <div className="col-span-2 text-text-dim italic text-center py-8">
                  No frames yet. Gemini begins generating after prompts are approved.
                </div>
              )}
            </div>

            {selected === 'review_frames' && pipeline.frames.length > 0 && (() => {
              const pending = pipeline.frames.filter(f => f.status === 'pending_review');
              const denied  = pipeline.frames.filter(f => f.status === 'denied');
              const approved = pipeline.frames.filter(f => f.status === 'approved' || f.final_url);
              return (
                <div className="mt-4 space-y-3">
                  {pending.length > 0 && (
                    <button onClick={() => onAction('pipeline_approve_frames', { frame_ids: 'all' })}
                      className="w-full py-2.5 rounded-lg bg-accent text-white font-bold text-[12px]
                                 hover:bg-accent-strong uppercase tracking-[0.15em]">
                      Approve All {pending.length} Pending
                    </button>
                  )}

                  <div className="pt-3 border-t border-dashed border-border/60">
                    <div className="text-[10px] uppercase tracking-[0.25em] font-bold text-text-dim mb-2">
                      After per-frame review
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        disabled={denied.length === 0}
                        onClick={() => onAction('user_command', {
                          text: `regenerate the ${denied.length} denied frames`,
                        })}
                        title="Gemma validates state, then calls pipeline_regenerate_frame on each denied frame."
                        style={{
                          padding: '10px 8px', borderRadius: 8, fontWeight: 700, fontSize: 11,
                          background: denied.length === 0 ? 'rgba(167,139,250,0.06)' : 'rgba(167,139,250,0.18)',
                          color: denied.length === 0 ? '#4e5872' : '#a78bfa',
                          border: `1px solid ${denied.length === 0 ? '#232d45' : 'rgba(167,139,250,0.35)'}`,
                          cursor: denied.length === 0 ? 'not-allowed' : 'pointer',
                          textTransform: 'uppercase', letterSpacing: '0.1em',
                        }}>
                        <div style={{ fontSize: 18, marginBottom: 2 }}>↻</div>
                        Regenerate
                        <div style={{ fontSize: 9, fontWeight: 500, opacity: 0.8, marginTop: 2,
                                      textTransform: 'none', letterSpacing: 0, fontFamily: 'JetBrains Mono, monospace' }}>
                          {denied.length} denied · via Gemma
                        </div>
                      </button>
                      <button
                        disabled={approved.length === 0}
                        onClick={() => onAction('pipeline_mark_complete')}
                        title="Mark this batch complete. Frames are already saved on disk; this advances the pipeline to 'done' and fires the archive. No Gemma routing — instant."
                        style={{
                          padding: '10px 8px', borderRadius: 8, fontWeight: 800, fontSize: 11,
                          background: approved.length === 0 ? 'rgba(16,185,129,0.06)' : '#10b981',
                          color: approved.length === 0 ? '#4e5872' : '#062d21',
                          border: `1px solid ${approved.length === 0 ? '#232d45' : 'rgba(16,185,129,0.5)'}`,
                          cursor: approved.length === 0 ? 'not-allowed' : 'pointer',
                          textTransform: 'uppercase', letterSpacing: '0.1em',
                        }}>
                        <div style={{ fontSize: 18, marginBottom: 2 }}>✓</div>
                        Save Batch
                        <div style={{ fontSize: 9, fontWeight: 600, opacity: 0.85, marginTop: 2,
                                      textTransform: 'none', letterSpacing: 0, fontFamily: 'JetBrains Mono, monospace' }}>
                          {approved.length} approved · mark complete
                        </div>
                      </button>
                    </div>
                    <div className="text-[10px] text-text-dim mt-2 italic leading-relaxed">
                      <strong>Save Batch</strong> is a direct action — instant, marks pipeline 'done', fires archive.
                      Regenerate routes through Gemma (slower, smarter).
                      {pending.length > 0 && ` ${pending.length} frame${pending.length>1?'s':''} still pending review.`}
                    </div>
                  </div>
                </div>
              );
            })()}
          </div>
        )}

        {selected === 'save' && (
          <div className="space-y-3">
            <div>Choose destination section. Multi-frame sections stack frames; single-item slots save one file.</div>
            <div>
              <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase',
                            letterSpacing: '0.2em', marginBottom: 6, fontWeight: 700 }}>
                Multi-frame sections
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {(config?.multi_frame_sections || ['Post1','Post2','Post3','Post4','Post5','Reel']).map(s => (
                  <button key={s} onClick={() => onAction('pipeline_save', { section: s })}
                    className="p-2 text-[12px] rounded bg-canvas border border-border-soft
                               hover:border-violet hover:bg-violet/10 text-text font-semibold transition">
                    {s}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase',
                            letterSpacing: '0.2em', marginBottom: 6, fontWeight: 700 }}>
                Single-item slots
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {(config?.single_item_slots || ['ReelCover','Story1','Story2','Story3']).map(s => (
                  <button key={s} onClick={() => onAction('pipeline_save', { section: s })}
                    className="p-2 text-[12px] rounded bg-canvas border border-border-soft
                               hover:border-accent hover:bg-accent/10 text-text font-semibold transition">
                    {s}
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}

        {selected === 'done' && (
          <div className="space-y-3">
            <div className="text-green font-semibold text-[14px]">
              ✓ {pipeline.frames.filter(f => f.final_url).length} files saved to disk.
            </div>
            {pipeline.frames.filter(f => f.final_url).map((f) => (
              <div key={f.id} className="text-[11px] font-mono text-text-dim">
                {f.final_path || f.final_url}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Frame lightbox — click a thumbnail → full-size preview with
          prompt text, metadata, and A/D/R keyboard hints. */}
      <AnimatePresence>
        {lightboxFrame !== null && pipeline.frames[lightboxFrame] && (() => {
          const f = pipeline.frames[lightboxFrame];
          const url = f.temp_url || f.final_url;
          const prompt = pipeline.prompts?.[lightboxFrame]?.text || '';
          const badgeColor = f.status === 'approved' || f.final_url ? '#10b981'
                           : f.status === 'denied' ? '#ef4444'
                           : f.status === 'generating' ? '#a78bfa'
                           : '#fbbf24';
          return (
            <motion.div
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              onClick={() => setLightboxFrame(null)}
              className="fixed inset-0 z-50 flex items-center justify-center p-8"
              style={{ background: 'rgba(5,7,13,0.88)', backdropFilter: 'blur(10px)' }}>
              <motion.div
                initial={{ scale: 0.95, y: 12, opacity: 0 }}
                animate={{ scale: 1, y: 0, opacity: 1 }}
                exit={{ scale: 0.95, y: 12, opacity: 0 }}
                onClick={(e) => e.stopPropagation()}
                className="max-w-[92vw] max-h-[92vh] grid gap-5"
                style={{ gridTemplateColumns: '1fr 340px' }}>
                {/* Image panel */}
                <div className="relative rounded-2xl overflow-hidden"
                  style={{ boxShadow: `0 30px 80px ${withAlpha(badgeColor, 0.25)}` }}>
                  {url ? (
                    <img src={url} alt={`Frame ${lightboxFrame+1}`}
                      className="w-full h-full object-contain"
                      style={{ maxHeight: '82vh', background: '#05070d' }} />
                  ) : (
                    <div className="flex items-center justify-center w-[640px] h-[800px] bg-[#05070d] text-[#7f8aa3]">
                      <ImageOff size={48} />
                    </div>
                  )}
                  {/* Prev/next */}
                  <button onClick={() => lightboxFrame > 0 && setLightboxFrame(lightboxFrame - 1)}
                    disabled={lightboxFrame === 0}
                    className="absolute left-3 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full flex items-center justify-center disabled:opacity-30"
                    style={{ background: 'rgba(10,15,28,0.7)', color: '#fff', backdropFilter: 'blur(8px)' }}>
                    <ChevronLeft size={18} />
                  </button>
                  <button onClick={() => lightboxFrame < pipeline.frames.length-1 && setLightboxFrame(lightboxFrame + 1)}
                    disabled={lightboxFrame >= pipeline.frames.length-1}
                    className="absolute right-3 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full flex items-center justify-center disabled:opacity-30"
                    style={{ background: 'rgba(10,15,28,0.7)', color: '#fff', backdropFilter: 'blur(8px)' }}>
                    <ChevronRight size={18} />
                  </button>
                  {/* Close */}
                  <button onClick={() => setLightboxFrame(null)}
                    className="absolute top-3 right-3 w-9 h-9 rounded-full flex items-center justify-center"
                    style={{ background: 'rgba(10,15,28,0.75)', color: '#fff' }}>
                    <X size={16} />
                  </button>
                </div>

                {/* Metadata panel */}
                <div className="rounded-2xl p-5 flex flex-col gap-4 overflow-y-auto"
                  style={{
                    background: 'rgba(10,15,28,0.75)',
                    backdropFilter: 'blur(24px)',
                    border: '1px solid rgba(255,255,255,0.08)',
                    maxHeight: '82vh',
                  }}>
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] uppercase tracking-[0.2em] text-text-dim">Frame</span>
                    <span className="text-[20px] font-bold text-white">{lightboxFrame + 1}</span>
                    <span className="text-[11px] text-text-dim">/ {pipeline.frames.length}</span>
                    <div className="flex-1" />
                    <span style={{
                      fontSize: 10, padding: '3px 9px', borderRadius: 999,
                      background: withAlpha(badgeColor, 0.18), color: badgeColor,
                      border: `1px solid ${withAlpha(badgeColor, 0.35)}`,
                      textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 600,
                    }}>{f.status}</span>
                  </div>

                  {prompt && (
                    <div>
                      <div className="flex items-center gap-2 mb-2">
                        <div className="text-[10px] uppercase tracking-widest text-text-dim font-bold flex-1">Prompt</div>
                        {lightboxEdit === null ? (
                          <button onClick={() => setLightboxEdit(f.prompt_text || prompt)}
                            title="Edit the prompt and regen this frame"
                            style={{
                              fontSize: 10, fontWeight: 700, padding: '3px 8px',
                              borderRadius: 4, color: '#a78bfa',
                              background: 'rgba(167,139,250,0.12)',
                              border: '1px solid rgba(167,139,250,0.35)',
                              cursor: 'pointer', letterSpacing: '0.08em',
                              textTransform: 'uppercase',
                            }}>✎ Edit</button>
                        ) : (
                          <button onClick={() => setLightboxEdit(null)}
                            style={{
                              fontSize: 10, fontWeight: 700, padding: '3px 8px',
                              borderRadius: 4, color: '#7f8aa3',
                              background: 'transparent',
                              border: '1px solid rgba(255,255,255,0.1)',
                              cursor: 'pointer', letterSpacing: '0.08em',
                              textTransform: 'uppercase',
                            }}>Cancel</button>
                        )}
                      </div>
                      {lightboxEdit === null ? (
                        <div className="text-[12px] leading-relaxed text-[#e6ebf5] max-h-60 overflow-y-auto font-mono"
                          style={{ background: 'rgba(5,7,13,0.5)', border: '1px solid rgba(255,255,255,0.05)', padding: 10, borderRadius: 8 }}>
                          {prompt}
                        </div>
                      ) : (
                        <div>
                          <textarea
                            value={lightboxEdit}
                            onChange={(e) => setLightboxEdit(e.target.value)}
                            autoFocus
                            style={{
                              width: '100%', minHeight: 200, padding: 10,
                              background: '#050810', borderRadius: 8,
                              border: '1px solid rgba(167,139,250,0.4)',
                              color: '#e6ebf5', fontFamily: 'ui-monospace, monospace',
                              fontSize: 12, lineHeight: 1.45, resize: 'vertical',
                              outline: 'none',
                            }} />
                          <button
                            onClick={() => {
                              const text = (lightboxEdit || '').trim();
                              if (!text) return;
                              onAction('pipeline_regen_frame', { id: f.id, text });
                              setLightboxEdit(null);
                            }}
                            disabled={!lightboxEdit?.trim()}
                            className="w-full mt-2 py-2 rounded-lg text-[11px] font-bold uppercase tracking-wider flex items-center justify-center gap-1.5"
                            style={{
                              background: lightboxEdit?.trim() ? '#a78bfa' : 'rgba(167,139,250,0.18)',
                              color: lightboxEdit?.trim() ? '#1a1538' : '#7f8aa3',
                              border: 'none',
                              cursor: lightboxEdit?.trim() ? 'pointer' : 'not-allowed',
                            }}>
                            <RefreshCw size={13} /> Save & Regen with Edited Prompt
                          </button>
                        </div>
                      )}
                    </div>
                  )}

                  {f.final_path && (
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-text-dim mb-1 font-bold">Path</div>
                      <div className="text-[10.5px] font-mono text-[#a5b4c9] break-all">{f.final_path}</div>
                    </div>
                  )}

                  {(selected === 'review_frames'
                    || pipeline?.stage === 'review_frames'
                    || pipeline?.stage === 'generating'
                    || pipeline?.stage === 'save') && (
                    <div className="flex items-center gap-2 pt-2 border-t border-white/5">
                      <button onClick={() => { onAction('pipeline_approve_frames', { frame_ids: [f.id] }); }}
                        className="flex-1 py-2 rounded-lg text-[11px] font-bold uppercase tracking-wider flex items-center justify-center gap-1.5"
                        style={{ background: 'rgba(16,185,129,0.18)', color: '#10b981', border: '1px solid rgba(16,185,129,0.35)' }}>
                        <Check size={13} />Approve
                      </button>
                      <button onClick={() => { onAction('pipeline_deny_frames', { frame_ids: [f.id] }); }}
                        className="flex-1 py-2 rounded-lg text-[11px] font-bold uppercase tracking-wider flex items-center justify-center gap-1.5"
                        style={{ background: 'rgba(239,68,68,0.18)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.35)' }}>
                        <X size={13} />Deny
                      </button>
                      <button onClick={() => { onAction('pipeline_regen_frame', { id: f.id }); }}
                        className="flex-1 py-2 rounded-lg text-[11px] font-bold uppercase tracking-wider flex items-center justify-center gap-1.5"
                        style={{ background: 'rgba(168,85,247,0.18)', color: '#a78bfa', border: '1px solid rgba(168,85,247,0.35)' }}>
                        <RefreshCw size={13} />Regen
                      </button>
                    </div>
                  )}

                  <div className="mt-auto pt-3 border-t border-white/5">
                    <div className="text-[10px] uppercase tracking-widest text-text-dim mb-2 font-bold">Shortcuts</div>
                    <div className="flex flex-wrap gap-1.5 text-[10px]">
                      {[
                        ['←', 'Prev'],['→', 'Next'],['A','Approve'],['D','Deny'],['R','Regen'],['Esc','Close'],
                      ].map(([k, t]) => (
                        <span key={k as string} className="flex items-center gap-1">
                          <kbd className="px-1.5 py-0.5 rounded text-[9px] font-mono font-bold"
                            style={{ background: 'rgba(255,255,255,0.08)', color: '#e6ebf5', border: '1px solid rgba(255,255,255,0.1)' }}>{k}</kbd>
                          <span className="text-text-dim">{t}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </motion.div>
            </motion.div>
          );
        })()}
      </AnimatePresence>
      {/* D6: per-child detail drawer. Opens when the user clicks a
          remote-host group header in the Agent Mesh strip. */}
      {openChild && pipeline?.agents && (() => {
        const host = openChild;
        const childAgents = pipeline.agents!.filter(a => a.remote && a.host === host);
        if (childAgents.length === 0) {
          // Host was deregistered while drawer was open — close ourselves.
          setTimeout(() => setOpenChild(null), 0);
          return null;
        }
        const fmtAge = (ts: number | null | undefined) => {
          if (!ts) return 'never';
          const s = Math.floor(Date.now() / 1000 - ts);
          if (s < 60) return `${s}s ago`;
          if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`;
          return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
        };
        const anyBusy = childAgents.some(a => a.busy > 0);
        const anyFailing = childAgents.some(a => (a.recent_failures || 0) > 0);
        const totalSuccess = childAgents.reduce((n, a) => n + a.success_count, 0);
        const totalFail = childAgents.reduce((n, a) => n + a.fail_count, 0);
        const latestPing = childAgents.reduce<number | null>(
          (m, a) => a.last_ping_ok && (!m || a.last_ping_ok > m) ? a.last_ping_ok : m,
          null,
        );
        const headerColor = !anyFailing ? '#10b981'
                          : anyBusy     ? '#fbbf24'
                                        : '#ef4444';
        return (
          <div style={{
            position: 'fixed', inset: 0, zIndex: 9998,
            background: 'rgba(5,8,16,0.65)',
            display: 'flex', alignItems: 'stretch', justifyContent: 'flex-end',
          }} onClick={() => setOpenChild(null)}>
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                background: '#0a0f1c', borderLeft: `3px solid ${headerColor}`,
                width: 420, maxWidth: '90vw', padding: 18,
                display: 'flex', flexDirection: 'column', gap: 12,
                overflowY: 'auto', color: '#e6ebf5', fontSize: 12,
                fontFamily: 'JetBrains Mono, monospace',
              }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontSize: 10, color: headerColor, fontWeight: 800,
                                  letterSpacing: '0.2em', textTransform: 'uppercase' }}>
                    🛰 Child · {anyBusy ? 'busy' : 'idle'}
                  </div>
                  <div style={{ fontSize: 15, color: '#e6ebf5', fontWeight: 700,
                                  marginTop: 4, wordBreak: 'break-all' }}>
                    {host}
                  </div>
                </div>
                <button onClick={() => setOpenChild(null)}
                  style={{ background: 'transparent', border: 'none', color: '#7f8aa3',
                           cursor: 'pointer', fontSize: 18, padding: 4 }}>×</button>
              </div>

              {/* Summary stats */}
              <div style={{ background: '#05080f', border: '1px solid #232d45',
                             borderRadius: 6, padding: '8px 10px', fontSize: 11 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ color: '#7f8aa3' }}>agents hosted</span>
                  <span style={{ color: '#e6ebf5', fontWeight: 700 }}>{childAgents.length}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3 }}>
                  <span style={{ color: '#7f8aa3' }}>total success / fail</span>
                  <span>
                    <span style={{ color: '#10b981' }}>✓{totalSuccess}</span>
                    {' '}
                    <span style={{ color: '#ef4444' }}>✗{totalFail}</span>
                  </span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3 }}>
                  <span style={{ color: '#7f8aa3' }}>last ping ok</span>
                  <span style={{ color: latestPing ? '#e6ebf5' : '#7f8aa3' }}>
                    {fmtAge(latestPing)}
                  </span>
                </div>
              </div>

              {/* Per-agent breakdown */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ fontSize: 9, color: '#7f8aa3', letterSpacing: '0.2em',
                                textTransform: 'uppercase', fontWeight: 700 }}>
                  Agents on this child
                </div>
                {childAgents.map((a, i) => {
                  const pf = a.ping_fails || 0;
                  const rf = a.recent_failures || 0;
                  const rowColor = pf >= 2 || rf >= 2 ? '#ef4444'
                                 : a.busy > 0         ? '#fbbf24'
                                 : '#10b981';
                  return (
                    <div key={i} style={{
                      background: '#05080f',
                      borderLeft: `3px solid ${rowColor}`,
                      padding: '6px 10px', fontSize: 11,
                    }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span style={{ color: '#e6ebf5', fontWeight: 700 }}>{a.name}</span>
                        <span style={{ color: '#7f8aa3', fontSize: 10 }}>
                          {a.busy > 0 ? `${a.busy} in flight` : 'idle'}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between',
                                      color: '#7f8aa3', fontSize: 10, marginTop: 2 }}>
                        <span>
                          <span style={{ color: '#10b981' }}>✓{a.success_count}</span>
                          {' '}
                          <span style={{ color: '#ef4444' }}>✗{a.fail_count}</span>
                          {rf > 0 && <span style={{ color: '#fbbf24' }}> ⚡{rf}</span>}
                          {pf > 0 && <span style={{ color: '#fbbf24' }}> pingfail{pf}</span>}
                        </span>
                        <span>
                          rate {a.success_rate === null
                            ? '—' : `${Math.round((a.success_rate||0)*100)}%`}
                        </span>
                      </div>
                      {a.last_error && (
                        <div style={{ color: '#ef4444', fontSize: 9.5, marginTop: 3,
                                        lineHeight: 1.35 }}>
                          last: {a.last_error.slice(0, 140)}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Actions */}
              <div style={{ marginTop: 'auto', display: 'flex', gap: 8 }}>
                <button onClick={() => {
                  onAction('direct_action', { action: 'child_ping', host_port: host });
                }}
                  style={{ flex: 1, padding: '10px', borderRadius: 5,
                           background: 'rgba(0,229,255,0.15)', color: '#00e5ff',
                           border: '1px solid rgba(0,229,255,0.35)', cursor: 'pointer',
                           fontSize: 11, fontWeight: 700, letterSpacing: '0.1em',
                           textTransform: 'uppercase' }}>
                  ↻ Re-ping now
                </button>
                <button onClick={() => {
                  if (confirm(`Disconnect ${host}?\n\nRemoves it from the mesh. If the child process is still running, it'll re-announce within ~30s.`)) {
                    onAction('direct_action', { action: 'child_disconnect', host_port: host });
                    setOpenChild(null);
                  }
                }}
                  style={{ flex: 1, padding: '10px', borderRadius: 5,
                           background: 'rgba(239,68,68,0.15)', color: '#ef4444',
                           border: '1px solid rgba(239,68,68,0.35)', cursor: 'pointer',
                           fontSize: 11, fontWeight: 700, letterSpacing: '0.1em',
                           textTransform: 'uppercase' }}>
                  ✗ Disconnect
                </button>
              </div>
              <div style={{ fontSize: 9, color: '#4e5872', marginTop: 4,
                              lineHeight: 1.4 }}>
                Re-ping: trigger an immediate liveness probe instead of waiting
                for the 30s auto-probe. Disconnect: remove from the mesh now;
                the child will re-announce via mDNS if its process is still up.
              </div>
            </div>
          </div>
        );
      })()}

      {uploadState && (() => {
        const s = uploadState;
        return (
          <div style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'rgba(5,8,16,0.75)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
            onClick={() => setUploadState(null)}>
            <div onClick={(e) => e.stopPropagation()}
              style={{
                background: '#0a0f1c', border: '1px solid #232d45',
                borderRadius: 10, padding: 20, minWidth: 360, maxWidth: 440,
                fontSize: 12, color: '#e6ebf5', boxShadow: '0 16px 48px rgba(0,0,0,0.6)',
              }}>
              <div style={{ fontSize: 11, fontWeight: 800, color: '#fbbf24',
                            textTransform: 'uppercase', letterSpacing: '0.18em',
                            marginBottom: 10 }}>
                ⚠ File already exists
              </div>
              <div style={{ color: '#e6ebf5', marginBottom: 6 }}>
                <code style={{ color: '#00e5ff' }}>{s.existingName}</code> is already in the target folder.
              </div>
              <div style={{ color: '#7f8aa3', fontSize: 11, marginBottom: 14 }}>
                Overwrite it, or save the upload under a different name.
              </div>
              <label style={{ display: 'block', fontSize: 10, color: '#7f8aa3',
                              textTransform: 'uppercase', letterSpacing: '0.15em',
                              marginBottom: 6 }}>
                Save as
              </label>
              <input
                defaultValue={s.defaultName}
                id="lotus-rename-input"
                style={{
                  width: '100%', padding: '8px 10px', fontSize: 12,
                  background: '#05080f', color: '#e6ebf5',
                  border: '1px solid #232d45', borderRadius: 6,
                  fontFamily: 'JetBrains Mono, monospace',
                }}
                autoFocus
              />
              <div style={{ display: 'flex', gap: 8, marginTop: 16, justifyContent: 'flex-end' }}>
                <button onClick={() => setUploadState(null)}
                  style={{ padding: '7px 14px', fontSize: 11, borderRadius: 5,
                           fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.12em',
                           background: 'transparent', color: '#7f8aa3',
                           border: '1px solid #232d45', cursor: 'pointer' }}>
                  Cancel
                </button>
                <button onClick={async () => {
                  const el = document.getElementById('lotus-rename-input') as HTMLInputElement | null;
                  const name = (el?.value || s.defaultName).trim();
                  if (!name) return;
                  const payload = { frameId: s.frameId, file: s.file };
                  setUploadState(null);
                  await tryUpload(payload.frameId, payload.file, { newName: name });
                }}
                  style={{ padding: '7px 14px', fontSize: 11, borderRadius: 5,
                           fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.12em',
                           background: 'rgba(0,229,255,0.18)', color: '#00e5ff',
                           border: '1px solid rgba(0,229,255,0.35)', cursor: 'pointer' }}>
                  Save As
                </button>
                <button onClick={async () => {
                  const payload = { frameId: s.frameId, file: s.file };
                  setUploadState(null);
                  await tryUpload(payload.frameId, payload.file, { force: true });
                }}
                  style={{ padding: '7px 14px', fontSize: 11, borderRadius: 5,
                           fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.12em',
                           background: 'rgba(239,68,68,0.18)', color: '#ef4444',
                           border: '1px solid rgba(239,68,68,0.35)', cursor: 'pointer' }}>
                  Overwrite
                </button>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}

// ApprovalSparkline — cumulative approval rate across frames in order,
// plotted as a small area chart. Grows left→right as frames land.
function ApprovalSparkline({ frames }: { frames: Array<{ status: string; final_url?: string }> }) {
  const W = 240, H = 40;
  if (!frames.length) return null;
  let approved = 0;
  const pts: { x: number; y: number; rate: number }[] = frames.map((f, i) => {
    if (f.status === 'approved' || f.final_url) approved += 1;
    const rate = approved / (i + 1);
    return { x: (i / Math.max(1, frames.length - 1)) * W, y: H - rate * H, rate };
  });
  const d = pts.length === 1
    ? `M0 ${H} L${W} ${pts[0].y}`
    : 'M ' + pts.map(p => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' L ');
  const area = d + ` L${W} ${H} L0 ${H} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
      style={{ display: 'block' }}>
      <defs>
        <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"  stopColor="#10b981" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#10b981" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#sparkFill)" />
      <path d={d} stroke="#10b981" strokeWidth="1.4" fill="none"
        strokeLinejoin="round" strokeLinecap="round"
        style={{ filter: 'drop-shadow(0 0 3px rgba(16,185,129,0.6))' }} />
      {/* Last point dot */}
      {pts.length > 0 && (
        <circle cx={pts[pts.length - 1].x} cy={pts[pts.length - 1].y} r={2.5}
          fill="#10b981"
          style={{ filter: 'drop-shadow(0 0 4px rgba(16,185,129,0.8))' }}>
          <animate attributeName="r" values="2.5;4;2.5" dur="1.8s" repeatCount="indefinite" />
        </circle>
      )}
    </svg>
  );
}

function MetricsSidebar({ pipeline, config, configErr, activity, connected, onAction }: {
  pipeline: PipelineState | null;
  config: LotusConfig | null;
  configErr?: string;
  activity: ActivityItem[];
  connected: boolean;
  onAction: (action: string, payload?: any) => void;
}) {
  const frames = pipeline?.frames || [];
  const approved = frames.filter(f => f.status === 'approved' || f.final_url).length;
  const saved = frames.filter(f => f.final_url).length;
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!pipeline?.created_at) return;
    const start = new Date(pipeline.created_at).getTime();
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [pipeline?.created_at]);

  const elapsedStr = elapsed
    ? `${String(Math.floor(elapsed/60)).padStart(2,'0')}:${String(elapsed%60).padStart(2,'0')}`
    : '—';

  const stat = (label: string, val: string|number, color='#e6ebf5') => (
    <div style={{ display:'flex', justifyContent:'space-between', fontSize: 11.5, padding: '5px 0' }}>
      <span style={{ color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.1em', fontSize: 10 }}>{label}</span>
      <span style={{ color, fontFamily: 'JetBrains Mono, monospace', fontWeight: 600 }}>{val}</span>
    </div>
  );

  return (
    <aside className="w-[280px] shrink-0 bg-surface border-l border-border flex flex-col overflow-hidden">
      <div className="p-4 border-b border-border-soft">
        <div className="text-[10px] uppercase tracking-[0.25em] font-bold text-accent mb-2">Pipeline</div>
        {pipeline ? (
          <>
            <div className="text-[15px] font-bold text-text mb-0.5">{pipeline.name}</div>
            <div className="text-[10px] text-text-dim font-mono">{pipeline.id}</div>
          </>
        ) : (
          <div className="text-[12px] text-text-dim italic">No active pipeline. Type a command or say "Lotus, start pipeline".</div>
        )}
      </div>

      {pipeline && (
        <div className="p-4 border-b border-border-soft">
          {stat('Stage',     pipeline.stage,                                    '#fbbf24')}
          {stat('Elapsed',   elapsedStr)}
          {stat('Prompts',   pipeline.prompts.length)}
          {stat('Generated', frames.filter(f => f.temp_url || f.final_url).length)}
          {stat('Approved',  approved, '#10b981')}
          {stat('Saved',     saved,    '#00e5ff')}
          {pipeline.save_section && stat('Section', pipeline.save_section, '#a78bfa')}
          {/* Approval-rate card: big percentage + sparkline of approval
              pace. Recomputes every frame event via pipeline.frames. */}
          {(frames.length > 0) && (
            <div className="mt-3 p-3 rounded-lg"
              style={{
                background: 'linear-gradient(135deg, rgba(16,185,129,0.06), rgba(0,229,255,0.05))',
                border: '1px solid rgba(16,185,129,0.18)',
              }}>
              <div className="flex items-baseline justify-between mb-1.5">
                <span className="text-[9px] uppercase tracking-[0.2em] text-text-dim font-semibold">Approval</span>
                <div className="flex items-baseline gap-1">
                  <span className="text-[22px] font-bold text-[#10b981] leading-none"
                    style={{ textShadow: '0 0 12px rgba(16,185,129,0.4)' }}>
                    {Math.round((approved / frames.length) * 100)}
                  </span>
                  <span className="text-[10px] text-text-dim">%</span>
                </div>
              </div>
              <ApprovalSparkline frames={frames} />
              <div className="text-[9px] text-text-dim mt-1 font-mono">
                {approved} / {frames.length} frames
              </div>
            </div>
          )}
          {pipeline.processing && (
            <div className="mt-2 text-[11px] text-accent flex items-center gap-2">
              <span className="animate-spin">⚙</span>
              {pipeline.processing_label || 'Working…'}
            </div>
          )}
          {pipeline.error && (
            <div className="mt-2 text-[11px] text-red-400">⚠ {pipeline.error}</div>
          )}
        </div>
      )}

      {!config && (
        <div className="p-4 border-b border-border-soft text-[10px]">
          {configErr
            ? <div className="text-red-400">Config error: {configErr}</div>
            : <div className="text-text-dim">Loading /api/config…</div>}
        </div>
      )}
      {config && (
        <div className="p-4 border-b border-border-soft">
          <div className="text-[10px] uppercase tracking-[0.25em] font-bold text-cyan mb-2">Destination</div>
          <div className="font-mono text-[11px] text-text break-all">{config.parent_folder}/</div>
          <div className="text-[10px] text-text-dim mt-1 break-all">{config.projects_root}</div>
          <div className="text-[10px] text-text-dim mt-2 mb-2">
            Active: <span className="text-text font-mono">{config.active_section}</span>
          </div>
          <div className="flex flex-wrap gap-1">
            {config.multi_frame_sections.map(s => (
              <span key={s} style={{
                fontSize: 9.5, padding: '2px 6px', borderRadius: 999,
                background: 'rgba(167,139,250,0.12)', color: '#a78bfa',
                border: '1px solid rgba(167,139,250,0.25)',
                fontFamily: 'JetBrains Mono, monospace',
              }}>{s}/</span>
            ))}
            {config.single_item_slots.map(s => (
              <span key={s} style={{
                fontSize: 9.5, padding: '2px 6px', borderRadius: 999,
                background: 'rgba(255,107,53,0.12)', color: '#ff6b35',
                border: '1px solid rgba(255,107,53,0.25)',
                fontFamily: 'JetBrains Mono, monospace',
              }}>{s}</span>
            ))}
          </div>
          <div className="mt-2 text-[10px] text-text-dim italic">
            FrameN{config.frame_ext} · resumable numbering
          </div>
        </div>
      )}

      {/* Live folder tree — visible on the Pipeline tab at all times so
          users can see files landing as Gemini writes them. */}
      {pipeline && (
        <div className="p-4 border-b border-border-soft">
          <PipelineFolderTree pipeline={pipeline} onAction={onAction} />
        </div>
      )}

      <div className="flex-1 overflow-hidden flex flex-col">
        <div className="p-4 pb-2 text-[10px] uppercase tracking-[0.25em] font-bold text-text-dim flex items-center gap-2">
          <span className={connected ? 'text-green' : 'text-red-400'}>●</span>
          Activity
        </div>
        <div className="flex-1 overflow-auto px-4 pb-4 space-y-1.5">
          {activity.length === 0 && (
            <div className="text-[11px] text-text-dim italic">No events yet.</div>
          )}
          {activity.map((a) => {
            const c = a.kind === 'cmd' ? '#ff6b35'
                    : a.kind === 'reply' ? '#00e5ff'
                    : a.kind === 'stage' ? '#fbbf24'
                    : '#7f8aa3';
            const t = new Date(a.ts).toLocaleTimeString('en-US', { hour12: false });
            return (
              <div key={a.id} className="text-[11px] leading-tight">
                <span style={{ color: '#4e5872', fontFamily: 'JetBrains Mono, monospace', fontSize: 9.5 }}>{t}</span>
                {' '}
                <span style={{ color: c, textTransform: 'uppercase', fontWeight: 700, fontSize: 9.5, letterSpacing: '0.1em' }}>
                  {a.kind}
                </span>
                <div className="text-text-dim truncate">{a.text}</div>
              </div>
            );
          })}
        </div>
      </div>
    </aside>
  );
}

// Two-click safety button — first click arms the button (shows CONFIRM?),
// second click within 3 s actually fires. Auto-disarms after 3 s of
// inactivity. Replaces window.confirm() which traps accidental mouse-clicks
// on Return/Enter. Per user request: "any popup should auto-click cancel
// and move forward" → the safe default is NOT to fire unless user
// deliberately clicks twice.
function ArmedButton({ label, armedLabel, onFire, color, title, bold = false }: {
  label: string; armedLabel: string; onFire: () => void;
  color: string; title?: string; bold?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  const click = () => {
    if (!armed) { setArmed(true); return; }
    setArmed(false);
    onFire();
  };
  return (
    <button onClick={click} title={title}
      style={{
        marginBottom: 6,
        padding: armed ? '4px 16px' : '4px 12px',
        fontSize: armed ? 11 : 10,
        fontWeight: bold || armed ? 800 : 700,
        background: armed ? `${color}44` : `${color}18`,
        color, border: `1px solid ${color}${armed ? 'cc' : '66'}`,
        boxShadow: armed ? `0 0 0 2px ${color}44, 0 6px 16px ${color}55` : 'none',
        borderRadius: 6, cursor: 'pointer',
        letterSpacing: armed ? '0.18em' : '0.1em',
        textTransform: 'uppercase',
        transition: 'all 0.15s',
        animation: armed ? 'pulse-soft 0.8s ease-in-out infinite' : 'none',
      }}>{armed ? armedLabel : label}</button>
  );
}

function CancelButton({ onCancel }: { onCancel: () => void }) {
  return (
    <ArmedButton label="Cancel" armedLabel="Click again to cancel"
      color="#fbbf24" onFire={onCancel}
      title="Two clicks required — cooperative cancel waits for current frame to finish." />
  );
}
function EmergencyStopButton({ onStop }: { onStop: () => void }) {
  return (
    <ArmedButton label="⛔ Emergency Stop" armedLabel="⛔ CLICK AGAIN TO KILL"
      color="#ef4444" onFire={onStop} bold
      title="TWO clicks required — hard kill, closes Chromium, stops every loop." />
  );
}

function VoiceStripTop({ state, lastReply }: {
  state: 'idle' | 'listening' | 'thinking' | 'speaking';
  lastReply: string;
}) {
  // Compact top strip — two inline lamps (LOTUS + GEMMA) side by side.
  // Lights up the active one, stays subtle otherwise. No huge card.
  const talking  = state === 'speaking';
  const thinking = state === 'thinking';
  const listening = state === 'listening';

  const lamp = (on: boolean, color: string, label: string, icon: string) => (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 10,
      padding: '7px 14px', borderRadius: 999,
      background: on ? `${color}1d` : 'rgba(20,26,40,0.5)',
      border: `1px solid ${on ? color + '88' : '#232d45'}`,
      boxShadow: on ? `0 0 0 1px ${color}33, 0 8px 20px ${color}22` : 'none',
      transition: 'all 0.22s ease',
    }}>
      <span style={{
        width: 22, height: 22, borderRadius: 6, fontSize: 13,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: on ? `${color}33` : 'rgba(255,255,255,0.03)',
        color: on ? color : '#4e5872',
      }} className={on ? 'animate-pulse-soft' : ''}>{icon}</span>
      <span style={{
        fontSize: 10.5, fontWeight: 800, letterSpacing: '0.2em',
        color: on ? color : '#4e5872', textTransform: 'uppercase',
      }}>{label}</span>
      <span style={{ display: 'inline-flex', alignItems: 'flex-end', gap: 2, height: 14 }}>
        {[0,1,2,3].map(i => (
          <span key={i} style={{
            display: 'inline-block', width: 2.5, borderRadius: 2,
            background: on ? color : '#2a3854',
            height: on ? undefined : 3,
            animation: on ? `lotus-eq 1.05s ${i*0.12}s ease-in-out infinite` : 'none',
          }}/>
        ))}
      </span>
    </div>
  );

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
      padding: '10px 16px', borderBottom: '1px solid #232d45',
      background: 'linear-gradient(90deg,#0a0f1c,#141a28,#0a0f1c)',
    }}>
      {lamp(talking,   '#ff6b35', 'LOTUS Talking',   '🗣')}
      {lamp(thinking,  '#a78bfa', 'GEMMA Thinking',  '🧠')}
      {listening && lamp(true, '#00e5ff', 'Mic Listening', '🎙')}
      {talking && lastReply && (
        <div style={{
          flex: 1, minWidth: 0,
          fontSize: 11, color: '#e6ebf5', fontStyle: 'italic',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          paddingLeft: 6, borderLeft: '1px dashed rgba(255,107,53,0.3)',
          marginLeft: 4,
        }}>“{lastReply}”</div>
      )}
    </div>
  );
}

type HistRun = {
  id: number;
  name: string;
  source_md: string;
  parent_folder: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  frame_count: number;
  approved_count: number;
};
type HistFrame = {
  id: number;
  run_id: number;
  post: string;
  frame_num: number;
  path: string;
  url: string;
  size: number;
  status: string;
  created_at: string;
};

type RecItem = { name: string; path: string; size: number; mtime: string };

function RecordingsTab({ recorder, onStart, onStop, onDelete }: {
  recorder: { active: boolean; elapsed_s: number; file: string | null };
  onStart: () => void;
  onStop: () => void;
  onDelete: (name: string) => void;
}) {
  const [items, setItems] = useState<RecItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [toDelete, setToDelete] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/recordings");
      const j = await r.json();
      setItems(j.recordings || []);
    } catch {} finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  // Refresh when the recorder flips (just stopped = new file appeared)
  useEffect(() => { refresh(); }, [recorder.active, refresh]);
  // Listen for backend broadcast
  useEffect(() => {
    const h = () => refresh();
    window.addEventListener("lotus:recordings_updated", h);
    return () => window.removeEventListener("lotus:recordings_updated", h);
  }, [refresh]);

  const fmtSize = (n: number) => n > 1024*1024 ? `${(n/1024/1024).toFixed(1)} MB` : `${Math.round(n/1024)} KB`;
  const fmtDate = (s: string) => { try { return new Date(s).toLocaleString(); } catch { return s; } };
  const fmtMMSS = (s: number) => `${Math.floor(s/60).toString().padStart(2,'0')}:${(s%60).toString().padStart(2,'0')}`;

  const totalBytes = items.reduce((a, b) => a + (b.size||0), 0);

  return (
    <div className="h-full w-full flex flex-col relative overflow-hidden">
      {/* Top hero bar */}
      <div className="m-6 mb-4 rounded-2xl p-5 relative overflow-hidden"
        style={{
          background: recorder.active
            ? 'linear-gradient(135deg, rgba(239,68,68,0.15) 0%, rgba(168,85,247,0.08) 100%)'
            : 'linear-gradient(135deg, rgba(0,229,255,0.06) 0%, rgba(168,85,247,0.06) 100%)',
          border: '1px solid rgba(255,255,255,0.08)',
          backdropFilter: 'blur(12px)',
        }}>
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <div className="flex items-center gap-3 mb-2">
              {recorder.active && (
                <motion.span
                  animate={{ opacity: [0.4, 1, 0.4], scale: [0.9, 1.15, 0.9] }}
                  transition={{ duration: 1.2, repeat: Infinity }}
                  className="w-3 h-3 rounded-full"
                  style={{ background: '#ef4444', boxShadow: '0 0 12px #ef4444' }} />
              )}
              <h1 className="text-[26px] font-bold tracking-tight"
                style={{
                  background: recorder.active
                    ? 'linear-gradient(90deg, #fca5a5 0%, #ef4444 100%)'
                    : 'linear-gradient(90deg, #ffffff 0%, #a5b4c9 100%)',
                  WebkitBackgroundClip: 'text',
                  WebkitTextFillColor: 'transparent',
                }}>
                {recorder.active ? `Recording — ${fmtMMSS(recorder.elapsed_s)}` : 'Recordings'}
              </h1>
            </div>
            <div className="text-[12px] text-[#a5b4c9] flex items-center gap-4">
              <span className="flex items-center gap-1.5"><Mic size={12} />{items.length} files</span>
              <span className="flex items-center gap-1.5"><FolderOpen size={12} />{fmtSize(totalBytes)}</span>
              {recorder.active && recorder.file && (
                <span className="flex items-center gap-1.5 font-mono">
                  <span className="w-1 h-1 bg-[#2a3448] rounded-full" />
                  writing to {recorder.file.split('/').pop()}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!recorder.active ? (
              <button onClick={onStart}
                className="px-5 py-2.5 rounded-lg font-semibold text-[12px] tracking-wider uppercase transition-all hover:-translate-y-px flex items-center gap-2"
                style={{
                  background: 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)',
                  color: 'white',
                  boxShadow: '0 8px 24px rgba(239,68,68,0.35)',
                }}>
                <Mic size={14} />Start Recording
              </button>
            ) : (
              <button onClick={onStop}
                className="px-5 py-2.5 rounded-lg font-semibold text-[12px] tracking-wider uppercase transition-all hover:-translate-y-px flex items-center gap-2"
                style={{
                  background: 'rgba(100,116,139,0.3)',
                  color: '#e6ebf5',
                  border: '1px solid rgba(148,163,184,0.3)',
                }}>
                <StopCircle size={14} />Stop
              </button>
            )}
            <button onClick={refresh}
              title="Refresh"
              className="w-10 h-10 rounded-lg flex items-center justify-center border border-[#232d45] hover:border-white/20 transition-colors text-[#7f8aa3] hover:text-white">
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            </button>
          </div>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-6 pb-6">
        {items.length === 0 && !loading && (
          <div className="mt-16 flex flex-col items-center text-[#7f8aa3]">
            <Mic size={48} className="text-[#2a3448] mb-3" />
            <div className="text-[14px]">No recordings yet.</div>
            <div className="text-[11px] mt-1">Click Start Recording, or say "start recording".</div>
          </div>
        )}
        <div className="space-y-2">
          {items.map((it, i) => (
            <motion.div key={it.path}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.02 }}
              className="rounded-xl p-4 flex items-center gap-4 transition-colors"
              style={{
                background: 'rgba(13,19,32,0.6)',
                border: '1px solid rgba(255,255,255,0.06)',
                backdropFilter: 'blur(8px)',
              }}>
              <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
                style={{
                  background: 'linear-gradient(135deg, #00e5ff33, #a855f733)',
                  border: '1px solid rgba(0,229,255,0.25)',
                }}>
                <Mic size={16} color="#00e5ff" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[13px] text-[#e6ebf5] truncate">{it.name}</div>
                <div className="text-[11px] text-[#7f8aa3] flex items-center gap-3 mt-1">
                  <span className="flex items-center gap-1"><Clock size={10} />{fmtDate(it.mtime)}</span>
                  <span>·</span>
                  <span>{fmtSize(it.size)}</span>
                </div>
              </div>
              <audio controls preload="metadata"
                src={`/recordings/${encodeURIComponent(it.name)}`}
                className="h-8"
                style={{ width: 260, colorScheme: 'dark' }} />
              <a href={`/recordings/${encodeURIComponent(it.name)}`}
                download={it.name}
                className="w-9 h-9 rounded-lg flex items-center justify-center hover:bg-white/10 text-[#7f8aa3] hover:text-white transition-colors"
                title="Download">
                <Download size={14} />
              </a>
              <button onClick={() => setToDelete(it.name)}
                title="Delete"
                className="w-9 h-9 rounded-lg flex items-center justify-center hover:bg-red-500/20 text-[#7f8aa3] hover:text-red-400 transition-colors">
                <Trash2 size={14} />
              </button>
            </motion.div>
          ))}
        </div>
      </div>

      {/* Delete confirm modal */}
      <AnimatePresence>
        {toDelete && (
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setToDelete(null)}
            className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
            style={{ backdropFilter: 'blur(6px)' }}>
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              onClick={e => e.stopPropagation()}
              className="max-w-md w-full rounded-2xl p-6"
              style={{
                background: 'rgba(13,19,32,0.95)',
                border: '1px solid rgba(239,68,68,0.4)',
                boxShadow: '0 30px 80px rgba(0,0,0,0.5), 0 0 60px rgba(239,68,68,0.2)',
              }}>
              <div className="flex items-center gap-3 mb-3">
                <div className="w-10 h-10 rounded-lg flex items-center justify-center"
                  style={{ background: 'rgba(239,68,68,0.2)' }}>
                  <Trash2 size={18} color="#ef4444" />
                </div>
                <h3 className="text-[16px] font-semibold text-white">Delete recording?</h3>
              </div>
              <p className="text-[13px] text-[#a5b4c9] mb-5">
                This will permanently delete <span className="font-mono text-[#e6ebf5]">{toDelete}</span>.
                Cannot be undone.
              </p>
              <div className="flex items-center justify-end gap-2">
                <button onClick={() => setToDelete(null)}
                  className="px-4 py-2 rounded-lg text-[12px] font-semibold text-[#a5b4c9] hover:bg-white/5 transition-colors">
                  Cancel
                </button>
                <button onClick={() => { onDelete(toDelete!); setToDelete(null); setTimeout(refresh, 300); }}
                  className="px-4 py-2 rounded-lg text-[12px] font-semibold text-white transition-all hover:-translate-y-px"
                  style={{ background: 'linear-gradient(135deg, #ef4444, #dc2626)', boxShadow: '0 4px 12px rgba(239,68,68,0.4)' }}>
                  Delete
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// CommandPalette — Linear/Raycast-style fuzzy command launcher. Cmd+K /
// Ctrl+K opens. Arrow keys navigate, Enter runs, Esc closes. Commands
// are static + context-aware (pipeline actions only appear when active).
function CommandPalette({ open, onClose, recorderActive, pipelineActive,
                         onNavigate, onAction, onCommand }: {
  open: boolean;
  onClose: () => void;
  recorderActive: boolean;
  pipelineActive: boolean;
  onNavigate: (tab: 'main'|'actions'|'pipeline'|'gallery'|'recordings') => void;
  onAction: (action: string, payload?: any) => void;
  onCommand: (text: string) => void;
}) {
  const [q, setQ] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) { setQ(''); setCursor(0); setTimeout(() => inputRef.current?.focus(), 50); }
  }, [open]);

  type Cmd = {
    id: string; label: string; hint?: string;
    group: 'Navigate' | 'Pipeline' | 'Recording' | 'Actions' | 'Quick';
    Icon: any;
    run: () => void;
  };
  const cmds: Cmd[] = [
    // Navigation
    { id: 'nav-home',       label: 'Go to Home',        group: 'Navigate', Icon: Home,     run: () => onNavigate('main') },
    { id: 'nav-actions',    label: 'Go to Actions',     group: 'Navigate', Icon: Zap,      run: () => onNavigate('actions') },
    { id: 'nav-gallery',    label: 'Go to Gallery',     group: 'Navigate', Icon: Library,  run: () => onNavigate('gallery') },
    { id: 'nav-recordings', label: 'Go to Recordings',  group: 'Navigate', Icon: Mic,      run: () => onNavigate('recordings') },
    ...(pipelineActive ? [{ id: 'nav-pipeline', label: 'Go to Pipeline', group: 'Navigate' as const, Icon: Workflow, run: () => onNavigate('pipeline') }] : []),

    // Recording
    ...(recorderActive
      ? [{ id: 'rec-stop',  label: 'Stop recording', hint: 'mic off', group: 'Recording' as const, Icon: StopCircle, run: () => onAction('rec_stop') }]
      : [{ id: 'rec-start', label: 'Start mic recording', hint: 'audio capture', group: 'Recording' as const, Icon: Mic, run: () => onAction('rec_start') }]),
    { id: 'rec-play-last', label: 'Play last recording', group: 'Recording', Icon: Sparkles, run: () => onAction('rec_play', { name: 'last' }) },

    // Pipeline
    ...(pipelineActive ? [
      { id: 'pipe-approve-all',  label: 'Approve all frames',  group: 'Pipeline' as const, Icon: Check,      run: () => onAction('pipeline_approve_frames', { frame_ids: 'all' }) },
      { id: 'pipe-cancel',       label: 'Cancel pipeline',     group: 'Pipeline' as const, Icon: X,          run: () => onAction('pipeline_cancel') },
      { id: 'pipe-stop',         label: 'Emergency stop',      group: 'Pipeline' as const, Icon: StopCircle, run: () => onAction('emergency_stop') },
    ] : []),

    // Quick commands (routed through voice/text command)
    { id: 'cmd-news', label: 'Check latest news', group: 'Quick', Icon: Activity, run: () => onCommand('latest news') },
    { id: 'cmd-today', label: 'Show today\'s generated images', group: 'Quick', Icon: ImageOff, run: () => onCommand('show today') },
    { id: 'cmd-gemini-status', label: 'Gemini API status', group: 'Actions', Icon: Sparkles, run: () => onAction('gemini_status') },
    { id: 'cmd-config', label: 'Reload config', group: 'Actions', Icon: RefreshCw, run: () => onAction('get_config') },
  ];

  const ql = q.toLowerCase().trim();
  const filtered = ql
    ? cmds.filter(c => c.label.toLowerCase().includes(ql) || c.group.toLowerCase().includes(ql) || (c.hint || '').toLowerCase().includes(ql))
    : cmds;
  // Clamp cursor
  const safeCursor = Math.max(0, Math.min(cursor, filtered.length - 1));

  // Group filtered commands by their `group` field for display
  const groups: Record<string, Cmd[]> = {};
  filtered.forEach(c => { (groups[c.group] ||= []).push(c); });

  const runAtIdx = (i: number) => {
    const c = filtered[i];
    if (c) c.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setCursor(x => Math.min(x + 1, filtered.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor(x => Math.max(x - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); runAtIdx(safeCursor); }
  };

  if (!open) return null;
  let flatIdx = -1;
  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose}
        className="fixed inset-0 z-[60] flex items-start justify-center pt-[12vh] px-4"
        style={{ background: 'rgba(5,7,13,0.72)', backdropFilter: 'blur(10px)' }}>
        <motion.div
          initial={{ opacity: 0, y: -8, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -8, scale: 0.97 }}
          transition={{ duration: 0.15 }}
          onClick={(e) => e.stopPropagation()}
          className="w-full max-w-[640px] rounded-2xl overflow-hidden"
          style={{
            background: 'rgba(10,15,28,0.94)',
            border: '1px solid rgba(255,255,255,0.08)',
            boxShadow: '0 30px 80px rgba(0,0,0,0.55), 0 0 60px rgba(0,229,255,0.08)',
            backdropFilter: 'blur(28px) saturate(180%)',
          }}>
          <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5">
            <Search size={16} className="text-[#7f8aa3]" />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => { setQ(e.target.value); setCursor(0); }}
              onKeyDown={onKey}
              placeholder="Type a command or search…"
              className="flex-1 bg-transparent text-[14px] text-[#e6ebf5] placeholder:text-[#53607a] outline-none" />
            <kbd className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold"
              style={{ background: 'rgba(255,255,255,0.08)', color: '#a5b4c9', border: '1px solid rgba(255,255,255,0.1)' }}>Esc</kbd>
          </div>

          <div className="max-h-[56vh] overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-4 py-10 text-center text-[#7f8aa3] text-[12px]">
                No commands match "{q}".
              </div>
            ) : (
              Object.entries(groups).map(([name, list]) => (
                <div key={name}>
                  <div className="px-4 pt-2 pb-1 text-[9px] uppercase tracking-[0.2em] font-semibold text-[#53607a]">
                    {name}
                  </div>
                  {list.map(c => {
                    flatIdx += 1;
                    const isActive = flatIdx === safeCursor;
                    return (
                      <button key={c.id}
                        onMouseEnter={() => setCursor(filtered.indexOf(c))}
                        onClick={() => c.run()}
                        className="w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors relative"
                        style={{
                          background: isActive ? 'rgba(0,229,255,0.08)' : 'transparent',
                          color: isActive ? '#e6ebf5' : '#a5b4c9',
                        }}>
                        {isActive && (
                          <motion.div layoutId="palette-bar"
                            className="absolute left-0 top-1 bottom-1 w-[2px] rounded-r"
                            style={{ background: '#00e5ff' }} />
                        )}
                        <c.Icon size={14} className={isActive ? 'text-[#00e5ff]' : 'text-[#7f8aa3]'} />
                        <span className="flex-1 text-[13px] font-medium">{c.label}</span>
                        {c.hint && (
                          <span className="text-[10px] text-[#53607a] font-mono">{c.hint}</span>
                        )}
                        {isActive && (
                          <kbd className="px-1.5 py-0.5 rounded text-[9px] font-mono font-bold"
                            style={{ background: 'rgba(0,229,255,0.15)', color: '#00e5ff', border: '1px solid rgba(0,229,255,0.3)' }}>↵</kbd>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>

          <div className="px-4 py-2 border-t border-white/5 flex items-center justify-between text-[10px] text-[#53607a]">
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1">
                <kbd className="px-1 py-0.5 rounded font-mono" style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.08)' }}>↑↓</kbd>
                navigate
              </span>
              <span className="flex items-center gap-1">
                <kbd className="px-1 py-0.5 rounded font-mono" style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.08)' }}>↵</kbd>
                run
              </span>
            </div>
            <span className="flex items-center gap-1">
              <kbd className="px-1 py-0.5 rounded font-mono" style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.08)' }}>⌘K</kbd>
              toggle
            </span>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function HeroView({ voiceState }: {
  voiceState: VoiceState; lastReply: string;
}) {
  // The Hero / Home tab is now always the calm voice orb. Gemma's active
  // state pops out into a separate GemmaFloater window (rendered at App
  // level) so the user can expand/collapse it while still seeing the
  // dashboard underneath.
  const isListening  = voiceState === 'listening';
  const isSpeaking   = voiceState === 'speaking';
  const isProcessing = voiceState === 'thinking';

  const ring = isSpeaking ? '#ff6b35' :
               isProcessing ? '#a855f7' :
               isListening ? '#00e5ff' : '#4d7fff';
  const ringSoft = `${ring}44`;

  return (
    <div className="h-full w-full relative flex items-center justify-center overflow-hidden">
      <AnimatePresence mode="wait">
        {true ? (
          // ─────────────── IDLE · voice-speaker waves ───────────────
          <motion.div
            key="voice"
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            transition={{ duration: 0.5 }}
            className="relative flex items-center justify-center"
            style={{ width: 'min(75vw, 680px)', aspectRatio: '1 / 1' }}>
            {/* Concentric speaker ripples */}
            {[0, 1, 2, 3].map((i) => (
              <motion.div key={i}
                initial={{ scale: 0.3, opacity: 0 }}
                animate={{ scale: [0.3, 1.35, 1.35], opacity: [0, 0.55, 0] }}
                transition={{
                  duration: 4.0,
                  repeat: Infinity,
                  delay: i * 1.0,
                  ease: "easeOut",
                }}
                className="absolute inset-0 rounded-full"
                style={{
                  border: `1.5px solid ${ring}`,
                  boxShadow: `0 0 50px ${ringSoft}`,
                }} />
            ))}
            {/* Central breathing orb */}
            <motion.div
              animate={{ scale: [1, 1.06, 1], opacity: [0.9, 1, 0.9] }}
              transition={{ duration: 2.8, repeat: Infinity, ease: "easeInOut" }}
              className="relative w-[42%] aspect-square rounded-full"
              style={{
                background: `radial-gradient(circle at 30% 30%, ${ring}aa 0%, ${ring}40 45%, transparent 75%), rgba(10,15,28,0.5)`,
                backdropFilter: 'blur(28px) saturate(180%)',
                WebkitBackdropFilter: 'blur(28px) saturate(180%)',
                border: `1px solid ${ring}66`,
                boxShadow: `
                  0 0 80px ${ringSoft},
                  inset 0 0 100px ${ringSoft},
                  inset 0 1px 0 rgba(255,255,255,0.15)
                `,
              }}>
              {/* Voice equalizer — horizontal bars inside orb */}
              <div className="absolute inset-0 flex items-center justify-center gap-1.5">
                {Array.from({ length: 13 }).map((_, i) => (
                  <motion.div key={i}
                    animate={{ scaleY: [0.3, 1, 0.3], opacity: [0.5, 1, 0.5] }}
                    transition={{
                      duration: 1.4 + (i % 4) * 0.25,
                      repeat: Infinity,
                      delay: i * 0.06,
                      ease: "easeInOut",
                    }}
                    className="w-[3px] rounded-full"
                    style={{
                      height: `${30 + (i % 5) * 8}%`,
                      background: `linear-gradient(180deg, ${ring}, ${ring}50)`,
                      boxShadow: `0 0 12px ${ring}aa`,
                    }} />
                ))}
              </div>
              {/* Inner gleam */}
              <div className="absolute inset-0 rounded-full pointer-events-none opacity-70"
                style={{ background: 'radial-gradient(circle at 30% 25%, rgba(255,255,255,0.25) 0%, transparent 45%)' }} />
            </motion.div>
            {/* Hint label */}
            <motion.div
              animate={{ opacity: [0.4, 0.75, 0.4] }}
              transition={{ duration: 3, repeat: Infinity }}
              className="absolute bottom-[-60px] left-1/2 -translate-x-1/2 text-[10px] uppercase tracking-[0.55em] text-[#7f8aa3] font-light whitespace-nowrap">
              say <span className="text-[#00e5ff] font-medium">"lotus"</span> to begin
            </motion.div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function GalleryTab() {
  const [runs, setRuns] = useState<HistRun[]>([]);
  const [stats, setStats] = useState<{runs: number; frames: number; approved: number} | null>(null);
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const [frames, setFrames] = useState<HistFrame[]>([]);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<HistFrame | null>(null);
  const [query, setQuery] = useState("");

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/history");
      const j = await r.json();
      setRuns(j.runs || []);
      setStats(j.stats || null);
      if (!selectedRun && j.runs?.length) setSelectedRun(j.runs[0].id);
    } catch {} finally { setLoading(false); }
  }, [selectedRun]);

  const fetchFrames = useCallback(async (runId: number) => {
    try {
      const r = await fetch(`/api/history/runs/${runId}`);
      const j = await r.json();
      setFrames(j.frames || []);
    } catch {}
  }, []);

  useEffect(() => { fetchRuns(); }, [fetchRuns]);
  useEffect(() => {
    if (selectedRun != null) fetchFrames(selectedRun);
  }, [selectedRun, fetchFrames]);

  // Keyboard nav: Esc closes preview, ←/→ navigate frames
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (!preview) return;
      if (e.key === "Escape") setPreview(null);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const idx = frames.findIndex(f => f.id === preview.id);
        const next = e.key === "ArrowRight" ? idx + 1 : idx - 1;
        if (next >= 0 && next < frames.length) setPreview(frames[next]);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [preview, frames]);

  const grouped = useMemo(() => {
    const m = new Map<string, HistFrame[]>();
    for (const f of frames) {
      if (!m.has(f.post)) m.set(f.post, []);
      m.get(f.post)!.push(f);
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0], undefined, {numeric: true}));
  }, [frames]);

  const filteredRuns = useMemo(() => {
    if (!query.trim()) return runs;
    const q = query.toLowerCase();
    return runs.filter(r => r.name.toLowerCase().includes(q) ||
                            r.parent_folder.toLowerCase().includes(q));
  }, [runs, query]);

  const fmtDate = (s: string) => {
    try {
      const d = new Date(s); const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      if (sameDay) return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
      return d.toLocaleDateString() + " " + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    } catch { return s; }
  };
  const fmtMB = (n: number) => n > 1024*1024 ? `${(n/1024/1024).toFixed(1)} MB` : n ? `${Math.round(n/1024)} KB` : "";

  const activeRun = runs.find(r => r.id === selectedRun);

  return (
    <div className="h-full flex relative overflow-hidden text-[#e6ebf5]"
      style={{background: '#05070d'}}>
      {/* ────────────── ANIMATED BG ──────────────*/}
      <div className="absolute inset-0 pointer-events-none z-0" aria-hidden="true">
        {/* Gradient mesh — three radial blobs that slowly drift */}
        <motion.div
          animate={{ x: [0, 60, -40, 0], y: [0, 40, -30, 0] }}
          transition={{ duration: 24, repeat: Infinity, ease: "easeInOut" }}
          className="absolute -top-40 -left-40 w-[520px] h-[520px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(0,229,255,0.18) 0%, transparent 60%)', filter: 'blur(40px)' }} />
        <motion.div
          animate={{ x: [0, -80, 50, 0], y: [0, 60, 20, 0] }}
          transition={{ duration: 30, repeat: Infinity, ease: "easeInOut" }}
          className="absolute top-1/3 right-0 w-[600px] h-[600px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(168,85,247,0.15) 0%, transparent 60%)', filter: 'blur(60px)' }} />
        <motion.div
          animate={{ x: [0, 40, -60, 0], y: [0, -40, 60, 0] }}
          transition={{ duration: 36, repeat: Infinity, ease: "easeInOut" }}
          className="absolute bottom-0 left-1/3 w-[500px] h-[500px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(77,127,255,0.12) 0%, transparent 60%)', filter: 'blur(50px)' }} />
        {/* Dot grid overlay for tactile feel */}
        <div className="absolute inset-0 opacity-[0.08]"
          style={{
            backgroundImage: 'radial-gradient(circle, #fff 1px, transparent 1px)',
            backgroundSize: '24px 24px',
          }} />
      </div>

      {/* ──────────────  LEFT RAIL (GLASS) ──────────────*/}
      <div className="w-[320px] shrink-0 border-r flex flex-col relative z-10"
        style={{
          background: 'rgba(10, 15, 28, 0.55)',
          backdropFilter: 'blur(24px) saturate(150%)',
          WebkitBackdropFilter: 'blur(24px) saturate(150%)',
          borderColor: 'rgba(255,255,255,0.06)',
        }}>
        {/* Stats header */}
        <div className="px-5 pt-6 pb-5" style={{borderBottom: '1px solid rgba(255,255,255,0.06)'}}>
          <div className="flex items-center gap-3 mb-5">
            <motion.div
              whileHover={{ rotate: 8, scale: 1.05 }}
              className="w-10 h-10 rounded-xl flex items-center justify-center relative overflow-hidden"
              style={{
                background: 'linear-gradient(135deg, #00e5ff 0%, #4d7fff 50%, #a855f7 100%)',
                boxShadow: '0 8px 24px rgba(0,229,255,0.35), inset 0 1px 0 rgba(255,255,255,0.3)',
              }}>
              <Library size={18} color="#0a0f1c" strokeWidth={2.5} />
              <div className="absolute inset-0 opacity-50"
                style={{background: 'linear-gradient(180deg, rgba(255,255,255,0.3) 0%, transparent 50%)'}} />
            </motion.div>
            <div>
              <div className="text-[17px] font-semibold tracking-tight"
                style={{
                  background: 'linear-gradient(90deg, #ffffff 0%, #a5b4c9 100%)',
                  WebkitBackgroundClip: 'text',
                  WebkitTextFillColor: 'transparent',
                }}>Gallery</div>
              <div className="text-[10px] text-[#7f8aa3] uppercase tracking-[0.18em] font-medium">Pipeline history</div>
            </div>
          </div>
          {stats && (
            <div className="grid grid-cols-3 gap-2">
              <StatChip icon={Activity} label="Runs" value={stats.runs} tone="cyan" />
              <StatChip icon={Image} label="Frames" value={stats.frames} tone="violet" />
              <StatChip icon={CheckCircle2} label="OK" value={stats.approved} tone="green" />
            </div>
          )}
        </div>

        {/* Search */}
        <div className="px-4 py-3 border-b border-[#1e2638]">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#7f8aa3]" />
            <input value={query} onChange={e => setQuery(e.target.value)}
              placeholder="Search runs…"
              className="w-full bg-[#0a0f1c] border border-[#1e2638] rounded-md pl-8 pr-3 py-1.5 text-[12px] placeholder:text-[#53607a] focus:border-[#00e5ff] focus:outline-none transition-colors" />
            <button onClick={fetchRuns}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded hover:bg-[#1e2638] transition-colors"
              title="Refresh">
              <RefreshCw size={12} className={loading ? "animate-spin text-[#00e5ff]" : "text-[#7f8aa3]"} />
            </button>
          </div>
        </div>

        {/* Runs list */}
        <div className="flex-1 overflow-y-auto">
          {filteredRuns.length === 0 && !loading && (
            <div className="p-8 text-center">
              <Inbox size={36} className="mx-auto text-[#2a3448] mb-2" />
              <div className="text-[12px] text-[#7f8aa3]">
                {query ? "No runs match" : "No pipeline runs yet"}
              </div>
            </div>
          )}
          <AnimatePresence initial={false}>
            {filteredRuns.map((r, i) => {
              const isActive = selectedRun === r.id;
              const dot = r.status === 'completed' ? '#10b981' :
                          r.status === 'running' ? '#f59e0b' : '#64748b';
              return (
                <motion.button key={r.id}
                  layout
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.02, duration: 0.15 }}
                  onClick={() => setSelectedRun(r.id)}
                  className={`w-full text-left px-4 py-3 border-b border-[#141a28] transition-colors relative ${
                    isActive ? 'bg-[#101828]' : 'hover:bg-[#0d1320]'
                  }`}>
                  {isActive && (
                    <motion.div layoutId="active-run-bar"
                      className="absolute left-0 top-1.5 bottom-1.5 w-[2px] rounded-r"
                      style={{background: '#00e5ff'}} />
                  )}
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span className="w-1.5 h-1.5 rounded-full shrink-0"
                              style={{background: dot, boxShadow: `0 0 6px ${dot}`}} />
                        <span className="font-medium text-[13px] truncate">{r.name}</span>
                      </div>
                      <div className="text-[11px] text-[#7f8aa3] font-mono truncate">
                        {r.parent_folder}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="text-[14px] font-semibold text-[#e6ebf5]">{r.frame_count}</div>
                      <div className="text-[9px] text-[#53607a] uppercase tracking-wider">frames</div>
                    </div>
                  </div>
                  {r.frame_count > 0 && (
                    <div className="mt-2 h-[3px] bg-[#1e2638] rounded-full overflow-hidden">
                      <div className="h-full rounded-full transition-all"
                        style={{
                          width: `${(r.approved_count / r.frame_count) * 100}%`,
                          background: 'linear-gradient(90deg, #10b981, #34d399)'
                        }} />
                    </div>
                  )}
                  <div className="text-[10px] text-[#53607a] mt-1.5 flex items-center justify-between">
                    <span>#{r.id}</span>
                    <span>{fmtDate(r.started_at)}</span>
                  </div>
                </motion.button>
              );
            })}
          </AnimatePresence>
        </div>
      </div>

      {/* ────────────── MAIN CONTENT ──────────────*/}
      <div className="flex-1 overflow-y-auto relative z-10">
        {selectedRun == null ? (
          <div className="h-full flex flex-col items-center justify-center text-[#7f8aa3]">
            <ImageOff size={48} className="mb-3 text-[#2a3448]" />
            <div className="text-[14px]">Select a pipeline run on the left.</div>
          </div>
        ) : frames.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-[#7f8aa3]">
            <ImageOff size={48} className="mb-3 text-[#2a3448]" />
            <div className="text-[14px]">No frames recorded for this run.</div>
          </div>
        ) : (
          <div className="p-8">
            {/* Hero-style Header */}
            <motion.div
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3 }}
              className="mb-8 p-6 rounded-2xl relative overflow-hidden"
              style={{
                background: 'linear-gradient(135deg, rgba(0,229,255,0.06) 0%, rgba(168,85,247,0.06) 100%)',
                border: '1px solid rgba(255,255,255,0.08)',
                backdropFilter: 'blur(12px)',
              }}>
              {/* Decorative gradient blob */}
              <div className="absolute top-0 right-0 w-64 h-64 pointer-events-none"
                style={{
                  background: 'radial-gradient(circle at 70% 30%, rgba(0,229,255,0.2) 0%, transparent 60%)',
                  filter: 'blur(30px)',
                }} />

              <div className="relative flex items-start justify-between gap-6">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 mb-2">
                    <h1 className="text-[32px] font-bold tracking-tight leading-none"
                      style={{
                        background: 'linear-gradient(90deg, #ffffff 0%, #00e5ff 50%, #a855f7 100%)',
                        WebkitBackgroundClip: 'text',
                        WebkitTextFillColor: 'transparent',
                        backgroundSize: '200% 100%',
                      }}>
                      {activeRun?.name || `Run #${selectedRun}`}
                    </h1>
                    {activeRun?.status === 'running' && (
                      <span className="text-[10px] px-2.5 py-1 rounded-full uppercase tracking-widest bg-amber-500/15 text-amber-400 border border-amber-500/30 flex items-center">
                        <span className="inline-block w-1.5 h-1.5 bg-amber-400 rounded-full animate-pulse mr-1.5" />
                        Running
                      </span>
                    )}
                    {activeRun?.status === 'completed' && (
                      <span className="text-[10px] px-2.5 py-1 rounded-full uppercase tracking-widest bg-green-500/15 text-green-400 border border-green-500/30 flex items-center gap-1.5">
                        <CheckCircle2 size={11} />Complete
                      </span>
                    )}
                  </div>
                  <div className="text-[13px] text-[#a5b4c9] flex items-center gap-4 mt-3 flex-wrap">
                    <span className="flex items-center gap-1.5">
                      <FolderOpen size={13} className="text-cyan-400" />{activeRun?.parent_folder}
                    </span>
                    <span className="w-1 h-1 bg-[#2a3448] rounded-full" />
                    <span className="flex items-center gap-1.5">
                      <Image size={13} className="text-violet-400" />{frames.length} frames
                    </span>
                    <span className="w-1 h-1 bg-[#2a3448] rounded-full" />
                    <span className="flex items-center gap-1.5">
                      <Grid3X3 size={13} className="text-blue-400" />{grouped.length} post{grouped.length === 1 ? '' : 's'}
                    </span>
                    <span className="w-1 h-1 bg-[#2a3448] rounded-full" />
                    <span className="flex items-center gap-1.5">
                      <Clock size={13} className="text-[#7f8aa3]" />{activeRun && fmtDate(activeRun.started_at)}
                    </span>
                  </div>
                </div>
                {/* Big numeric stat */}
                <div className="text-right">
                  <div className="text-[56px] font-bold leading-none tracking-tight"
                    style={{
                      background: 'linear-gradient(180deg, #ffffff 0%, #00e5ff 100%)',
                      WebkitBackgroundClip: 'text',
                      WebkitTextFillColor: 'transparent',
                    }}>
                    {activeRun?.approved_count || 0}
                  </div>
                  <div className="text-[10px] text-[#7f8aa3] uppercase tracking-widest mt-1">Approved / {frames.length}</div>
                </div>
              </div>
            </motion.div>

            {/* Grouped frames */}
            {grouped.map(([post, items], gi) => (
              <motion.div key={post}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: gi * 0.04, duration: 0.2 }}
                className="mb-8">
                <div className="flex items-center gap-3 mb-3 pb-2 border-b border-[#1e2638]">
                  <div className="text-[14px] font-semibold tracking-tight">{post}</div>
                  <div className="text-[11px] text-[#7f8aa3]">{items.length} / 10</div>
                  <div className="flex-1" />
                  <div className="flex items-center gap-1 text-[10px] text-[#7f8aa3]">
                    {items.filter(f => f.status === 'approved').length > 0 && (
                      <span className="flex items-center gap-1 text-green-400">
                        <CheckCircle2 size={11} />
                        {items.filter(f => f.status === 'approved').length}
                      </span>
                    )}
                  </div>
                </div>
                <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))' }}>
                  {items.map((f, fi) => (
                    <motion.button key={f.id}
                      initial={{ opacity: 0, scale: 0.92 }}
                      animate={{ opacity: 1, scale: 1 }}
                      transition={{ delay: fi * 0.025, duration: 0.25, ease: "easeOut" }}
                      whileHover={{ y: -6, transition: { duration: 0.15 } }}
                      onClick={() => setPreview(f)}
                      className="group relative aspect-[4/5] rounded-xl overflow-hidden cursor-pointer transition-all duration-300"
                      style={{
                        background: 'rgba(13,19,32,0.6)',
                        backdropFilter: 'blur(8px)',
                        border: '1px solid rgba(255,255,255,0.06)',
                        boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
                      }}
                      onMouseEnter={(e) => {
                        (e.currentTarget as HTMLElement).style.boxShadow =
                          '0 12px 40px rgba(0,229,255,0.25), 0 0 0 1px rgba(0,229,255,0.4)';
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLElement).style.boxShadow = '0 4px 12px rgba(0,0,0,0.4)';
                      }}>
                      {f.url ? (
                        <motion.img src={f.url}
                          alt={`Frame ${f.frame_num}`}
                          loading="lazy"
                          className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
                        />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-[#53607a]">
                          <ImageOff size={24} />
                        </div>
                      )}
                      {/* Top-left frame # */}
                      <div className="absolute top-2 left-2 bg-black/75 backdrop-blur-sm text-white text-[10px] px-2 py-1 rounded-md font-mono">
                        {f.frame_num}
                      </div>
                      {/* Top-right status */}
                      {f.status === 'approved' && (
                        <div className="absolute top-2 right-2 w-6 h-6 rounded-full bg-green-500/90 flex items-center justify-center">
                          <Check size={13} color="white" strokeWidth={3} />
                        </div>
                      )}
                      {f.status === 'denied' && (
                        <div className="absolute top-2 right-2 w-6 h-6 rounded-full bg-red-500/90 flex items-center justify-center">
                          <X size={13} color="white" strokeWidth={3} />
                        </div>
                      )}
                      {/* Bottom gradient + size */}
                      <div className="absolute inset-x-0 bottom-0 p-2 bg-gradient-to-t from-black/90 via-black/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
                        <div className="flex items-center justify-between text-white text-[10px]">
                          <span className="font-mono">{fmtMB(f.size)}</span>
                          <Maximize2 size={11} />
                        </div>
                      </div>
                    </motion.button>
                  ))}
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </div>

      {/* ────────────── PREVIEW LIGHTBOX ──────────────*/}
      <AnimatePresence>
        {preview && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={() => setPreview(null)}
            className="fixed inset-0 bg-black/85 z-50 flex items-center justify-center p-8"
            style={{ backdropFilter: 'blur(8px)' }}>
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="relative max-w-[95vw] max-h-[90vh]"
              onClick={e => e.stopPropagation()}>
              <img src={preview.url}
                alt={`${preview.post} Frame ${preview.frame_num}`}
                className="max-w-full max-h-[82vh] object-contain rounded-lg"
                style={{ boxShadow: '0 20px 60px rgba(0,229,255,0.15)' }} />
              <div className="mt-4 flex items-center justify-between gap-4 px-1">
                <div>
                  <div className="text-white font-semibold text-[15px]">
                    {preview.post} · Frame {preview.frame_num}
                  </div>
                  <div className="text-white/50 text-[11px] font-mono">
                    {fmtMB(preview.size)} · {preview.status}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => {
                      const idx = frames.findIndex(f => f.id === preview.id);
                      if (idx > 0) setPreview(frames[idx - 1]);
                    }}
                    className="p-2 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors"
                    title="Previous (←)">
                    <ChevronLeft size={18} />
                  </button>
                  <button
                    onClick={() => {
                      const idx = frames.findIndex(f => f.id === preview.id);
                      if (idx < frames.length - 1) setPreview(frames[idx + 1]);
                    }}
                    className="p-2 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors"
                    title="Next (→)">
                    <ChevronRight size={18} />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      // Frame is already saved to the project folder during
                      // generation — reveal it in Finder instead of popping a
                      // save-location dialog (which the old download link did
                      // in both the desktop app and the browser).
                      fetch('/api/reveal', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: preview.path || preview.url }),
                      }).catch(() => {});
                    }}
                    className="p-2 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors flex items-center justify-center"
                    title="Reveal in Finder — already saved to the project folder">
                    <FolderOpen size={18} />
                  </button>
                  <button
                    onClick={() => setPreview(null)}
                    className="p-2 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors"
                    title="Close (Esc)">
                    <X size={18} />
                  </button>
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

interface PipelineRow {
  id: string;
  name: string;
  source_md: string | null;
  source_basename: string;
  source_hash: string | null;
  stage: string;
  paused: boolean;
  cancelled: boolean;
  created_at: number;
  updated_at: number;
  prompt_counts: { pending: number; approved: number; rejected: number };
  folder_name: string | null;   // A10 — null on legacy pipelines (pre-A10),
                                // they fall back to <brand><MMDDYYYY>
}

function PipelinesOverview({ onFocus, onReopen, onCreate, onDelete, onBackfill, liveIds }: {
  onFocus: (id: string) => void;
  onReopen: (id: string) => void;
  onCreate: (name: string) => void;
  onDelete: (id: string, name: string) => void;
  onBackfill: (id: string, name: string) => void;
  liveIds: Set<string>;
}) {
  const [rows, setRows] = useState<PipelineRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  // When set, the read-only saved-frames gallery is open for this pipeline.
  // Lets finished/cancelled pipelines (not in the live registry) be reviewed
  // without re-entering the live focused view.
  const [viewing, setViewing] = useState<PipelineRow | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch("/api/pipelines");
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      setRows(j.pipelines || []);
    } catch (e: any) {
      setErr(e?.message || "fetch failed");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);
  useEffect(() => {
    const id = setInterval(fetchAll, 5000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const stageColor = (stage: string, paused: boolean, cancelled: boolean): string => {
    if (cancelled)            return '#7f8aa3';
    if (paused)               return '#fbbf24';
    if (stage === 'done')     return '#10b981';
    if (stage === 'generating')    return '#a78bfa';
    if (stage === 'review_frames') return '#00e5ff';
    if (stage === 'review_prompts')return '#00e5ff';
    return '#7f8aa3';
  };

  const formatTs = (ts: number): string => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  };

  const total = rows.length;
  const live = rows.filter(r => !r.cancelled && r.stage !== 'done').length;
  const done = rows.filter(r => !r.cancelled && r.stage === 'done').length;
  const cancelled = rows.filter(r => r.cancelled).length;

  return (
    <div className="h-full flex flex-col p-5 gap-4 overflow-hidden">
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="text-[15px] font-semibold text-text">All Pipelines</h2>
          <p className="text-[11px] text-text-dim mt-0.5">
            {total} total · {live} live · {done} done · {cancelled} cancelled
          </p>
        </div>
        <div className="flex items-center gap-2">
          {creating ? (
            <div className="flex items-center gap-1.5">
              <input
                autoFocus
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    const n = newName.trim();
                    if (n) { onCreate(n); setCreating(false); setNewName(''); }
                  }
                  if (e.key === 'Escape') { setCreating(false); setNewName(''); }
                }}
                placeholder="Pipeline name…"
                style={{
                  background: '#0a0f1c', border: '1px solid #10b981',
                  borderRadius: 6, padding: '6px 10px',
                  color: '#e6ebf5', fontSize: 12, outline: 'none', minWidth: 200,
                }} />
              <button onClick={() => {
                const n = newName.trim();
                if (n) { onCreate(n); setCreating(false); setNewName(''); }
              }}
                disabled={!newName.trim()}
                className="px-3 py-1.5 rounded-md text-[11px] font-semibold transition-colors"
                style={{
                  background: newName.trim() ? '#10b981' : '#1a2333',
                  color: newName.trim() ? '#05130a' : '#53607a',
                  cursor: newName.trim() ? 'pointer' : 'not-allowed',
                  border: 'none',
                }}>Create</button>
              <button onClick={() => { setCreating(false); setNewName(''); }}
                className="text-[11px] text-text-dim hover:text-text px-2 py-1.5">
                Cancel
              </button>
            </div>
          ) : (
            <button onClick={() => setCreating(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-semibold transition-colors"
              style={{
                background: 'rgba(16,185,129,0.12)',
                color: '#10b981',
                border: '1px solid rgba(16,185,129,0.4)',
              }}>
              + New Pipeline
            </button>
          )}
          <button onClick={fetchAll} disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] text-text-dim hover:text-text border border-border-soft hover:border-border transition-colors">
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {err && (
        <div className="text-[12px] text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
          {err}
        </div>
      )}

      {!loading && !err && rows.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center text-text-dim text-[13px] gap-2">
          <Inbox size={32} strokeWidth={1.5} />
          <div>No pipelines yet. Start one from the Pipeline tab.</div>
        </div>
      )}

      <div className="grid gap-3 overflow-y-auto pr-1"
           style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
        {rows.map((p) => {
          const tone = stageColor(p.stage, p.paused, p.cancelled);
          const isLive = liveIds.has(p.id);
          return (
            <div key={p.id}
              className="rounded-lg p-4 transition-all relative"
              style={{
                background: '#0a0f1c',
                border: `1px solid ${withAlpha(tone, 0.35)}`,
                boxShadow: isLive ? `0 0 0 1px ${withAlpha(tone, 0.15)}` : 'none',
                opacity: p.cancelled ? 0.55 : 1,
              }}>
              {/* Trash button — top-right, absolute so it doesn't disturb
                  the rest of the card layout. Confirm-on-click. */}
              <button onClick={(e) => {
                e.stopPropagation();
                if (confirm(`Delete pipeline "${p.name}"?\n\nThis removes the row from pipelines.db. Prompts data (prompts.db) is kept — re-loading the same source MD will resurface them.`)) {
                  onDelete(p.id, p.name);
                }
              }}
                title="Delete this pipeline (cancels live, removes from DB)"
                style={{
                  position: 'absolute', top: 8, right: 8, zIndex: 10,
                  width: 26, height: 26, borderRadius: 4,
                  border: '1px solid rgba(239,68,68,0.25)',
                  background: 'rgba(10,15,28,0.85)',
                  color: '#ef4444',
                  cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  opacity: 0.85,
                }}
                onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; e.currentTarget.style.background = 'rgba(239,68,68,0.18)'; }}
                onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.85'; e.currentTarget.style.background = 'rgba(10,15,28,0.85)'; }}>
                <Trash2 size={13} />
              </button>
              <button
                onClick={() => { if (isLive) onFocus(p.id); else setViewing(p); }}
                title={isLive ? 'Click to focus this pipeline' : 'Click to view saved frames (read-only)'}
                className="text-left w-full"
                style={{
                  background: 'transparent',
                  border: 'none',
                  padding: 0,
                  cursor: 'pointer',
                  color: 'inherit',
                }}>
              <div className="flex items-start justify-between gap-2 mb-2 pr-9">
                <div className="min-w-0">
                  <div className="text-[13px] font-semibold text-text truncate">{p.name}</div>
                  <div className="text-[10px] text-text-dim font-mono truncate mt-0.5">{p.id}</div>
                </div>
                <span className="px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wider shrink-0"
                      style={{
                        background: withAlpha(tone, 0.12),
                        color: tone,
                        border: `1px solid ${withAlpha(tone, 0.4)}`,
                      }}>
                  {p.cancelled ? 'cancelled' : (p.paused ? 'paused' : p.stage)}
                </span>
              </div>

              {p.source_basename && (
                <div className="flex items-center gap-1.5 text-[11px] text-text-dim mb-2">
                  <FileText size={11} />
                  <span className="truncate">{p.source_basename}</span>
                </div>
              )}

              <div className="flex items-center gap-2 text-[10px] text-text-dim mb-2">
                <Clock size={10} />
                <span>created {formatTs(p.created_at)}</span>
                {p.updated_at && p.updated_at > p.created_at + 5 && (
                  <>
                    <span className="opacity-50">·</span>
                    <span>updated {formatTs(p.updated_at)}</span>
                  </>
                )}
              </div>

              {/* Folder row — show per-pipeline folder if set; else
                  offer Migrate (A11) for legacy pipelines. */}
              <div className="flex items-center gap-1.5 text-[10px] text-text-dim mb-1">
                <FolderOpen size={10} />
                {p.folder_name ? (
                  <span style={{ fontFamily: 'JetBrains Mono, monospace', color: '#10b981' }}>
                    {p.folder_name}
                  </span>
                ) : (
                  <>
                    <span style={{ color: '#7f8aa3' }}>shared brand folder</span>
                    {liveIds.has(p.id) && !p.cancelled && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (confirm(`Migrate "${p.name}" to its own folder?\n\nFuture renders will go to a new <name>_<MMDDYYYY> folder. Past renders stay in Techengine<MMDDYYYY> (no files moved).`)) {
                            onBackfill(p.id, p.name);
                          }
                        }}
                        title="Give this legacy pipeline its own folder"
                        style={{
                          background: 'transparent', border: 'none', padding: 0,
                          marginLeft: 4, color: '#fbbf24', fontSize: 10,
                          textDecoration: 'underline', cursor: 'pointer',
                        }}>
                        migrate →
                      </button>
                    )}
                  </>
                )}
              </div>
              {(p.prompt_counts.pending + p.prompt_counts.approved + p.prompt_counts.rejected) > 0 && (
                <div className="flex items-center gap-2 text-[10px] mt-2">
                  <span className="text-text-dim">prompts:</span>
                  {p.prompt_counts.approved > 0 && (
                    <span style={{ color: '#10b981' }}>{p.prompt_counts.approved} approved</span>
                  )}
                  {p.prompt_counts.pending > 0 && (
                    <span style={{ color: '#fbbf24' }}>{p.prompt_counts.pending} pending</span>
                  )}
                  {p.prompt_counts.rejected > 0 && (
                    <span className="text-text-dim">{p.prompt_counts.rejected} rejected</span>
                  )}
                </div>
              )}
              </button>
            </div>
          );
        })}
      </div>

      {viewing && (
        <FinishedPipelineGallery row={viewing} onClose={() => setViewing(null)} onReopen={onReopen} />
      )}
    </div>
  );
}

// Read-only gallery for a pipeline's saved frames. Used by the A2 overview to
// review finished/cancelled pipelines that are no longer in the live registry.
// Pulls straight from disk via /api/pipelines/<id>/frames (keyed by folder_name)
// so it works regardless of whether the pipeline is still live.
function FinishedPipelineGallery({ row, onClose, onReopen }: {
  row: PipelineRow;
  onClose: () => void;
  onReopen: (id: string) => void;
}) {
  const [frames, setFrames] = useState<{ name: string; section: string; url: string }[]>([]);
  const [folder, setFolder] = useState('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true); setErr(null);
    fetch(`/api/pipelines/${encodeURIComponent(row.id)}/frames`)
      .then(r => r.json())
      .then(j => {
        if (!alive) return;
        if (j.error) throw new Error(j.error);
        setFrames(j.frames || []);
        setFolder(j.folder || '');
      })
      .catch((e: any) => { if (alive) setErr(e?.message || 'load failed'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [row.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Preserve the backend ordering (sectioned first) while grouping for display.
  const sections: string[] = [];
  for (const f of frames) {
    const key = f.section || '(top level)';
    if (!sections.includes(key)) sections.push(key);
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        background: 'rgba(3,6,15,0.82)', backdropFilter: 'blur(3px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
      }}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#0a0f1c', border: '1px solid #1a2333', borderRadius: 12,
          width: 'min(1100px, 94vw)', maxHeight: '88vh',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          boxShadow: '0 24px 80px rgba(0,0,0,0.55)',
        }}>
        <div className="flex items-start justify-between gap-3"
             style={{ padding: '16px 18px', borderBottom: '1px solid #1a2333' }}>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-[14px] font-semibold text-text truncate">{row.name}</h3>
              <span className="px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wider shrink-0"
                    style={{ background: 'rgba(127,138,163,0.12)', color: '#7f8aa3', border: '1px solid rgba(127,138,163,0.3)' }}>
                read-only
              </span>
            </div>
            <div className="text-[11px] text-text-dim mt-0.5 truncate" style={{ fontFamily: 'JetBrains Mono, monospace' }}>
              {folder || row.folder_name || '(no folder)'} · {frames.length} frame{frames.length === 1 ? '' : 's'}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => { onReopen(row.id); onClose(); }}
              title="Reopen this pipeline in the Pipeline view so you can regenerate individual frames"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-semibold transition-colors"
              style={{
                background: 'rgba(0,229,255,0.12)', color: '#00e5ff',
                border: '1px solid rgba(0,229,255,0.4)', cursor: 'pointer',
              }}>
              <RefreshCw size={12} /> Reopen to regenerate
            </button>
            <button onClick={onClose}
              title="Close (Esc)"
              style={{
                width: 30, height: 30, borderRadius: 6, border: '1px solid #1a2333',
                background: 'transparent', color: '#7f8aa3', cursor: 'pointer',
                fontSize: 15, lineHeight: 1,
              }}>✕</button>
          </div>
        </div>

        <div style={{ overflowY: 'auto', padding: 18 }}>
          {loading && (
            <div className="text-[12px] text-text-dim text-center" style={{ padding: '40px 0' }}>
              Loading saved frames…
            </div>
          )}
          {err && (
            <div className="text-[12px] text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
              {err}
            </div>
          )}
          {!loading && !err && frames.length === 0 && (
            <div className="text-[12px] text-text-dim text-center" style={{ padding: '40px 0' }}>
              No saved frames found on disk for this pipeline.
            </div>
          )}
          {!loading && !err && sections.map(sec => (
            <div key={sec} style={{ marginBottom: 18 }}>
              {sections.length > 1 && (
                <div className="text-[11px] font-semibold text-text-dim uppercase tracking-wider" style={{ marginBottom: 8 }}>
                  {sec}
                </div>
              )}
              <div className="grid gap-3"
                   style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))' }}>
                {frames.filter(f => (f.section || '(top level)') === sec).map(f => (
                  <button key={f.url}
                    onClick={() => window.open(f.url, '_blank')}
                    title={`${f.name} — open full size`}
                    style={{
                      background: '#05080f', border: '1px solid #1a2333', borderRadius: 8,
                      overflow: 'hidden', cursor: 'pointer', padding: 0, textAlign: 'left',
                    }}>
                    <img src={f.url} alt={f.name} loading="lazy"
                         style={{ width: '100%', aspectRatio: '1 / 1', objectFit: 'cover', display: 'block' }} />
                    <div className="text-[10px] text-text-dim truncate" style={{ padding: '5px 7px' }}>
                      {f.name}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface QueueFrame {
  id: string;
  prompt_text: string;
  section?: string | null;
  slide_idx?: number | null;
  status: string;
  source?: string | null;
  error?: string | null;
  retry_count?: number;
  final_url?: string;
  temp_url?: string;
}
interface QueueUpcoming {
  id: string;
  title?: string | null;
  topic_number?: number | null;
  slide_number?: number | null;
  text: string;
}
interface QueuePipeline {
  id: string;
  name: string;
  stage: string;
  paused: boolean;
  cancelled: boolean;
  frame_count: number;
  upcoming_count: number;
  frames: QueueFrame[];
  upcoming: QueueUpcoming[];
}

function QueueView() {
  const [data, setData] = useState<QueuePipeline[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const fetchQueue = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch("/api/queue");
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      setData(j.pipelines || []);
    } catch (e: any) {
      setErr(e?.message || "fetch failed");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchQueue(); }, [fetchQueue]);
  useEffect(() => {
    const id = setInterval(fetchQueue, 3000);
    return () => clearInterval(id);
  }, [fetchQueue]);

  const statusColor = (s: string): string => {
    if (s === 'generating')          return '#a78bfa';
    if (s === 'pending_review')      return '#fbbf24';
    if (s === 'approved')            return '#10b981';
    if (s === 'denied' || s === 'rejected') return '#ef4444';
    if (s === 'failed')              return '#ef4444';
    return '#7f8aa3';
  };

  // Roll-ups across every pipeline
  const totals = useMemo(() => {
    let frames = 0, generating = 0, pending = 0, approved = 0, failed = 0, upcoming = 0;
    for (const p of data) {
      frames   += p.frame_count;
      upcoming += p.upcoming_count;
      for (const f of p.frames) {
        if (f.status === 'generating')      generating++;
        else if (f.status === 'pending_review') pending++;
        else if (f.status === 'approved')   approved++;
        else if (f.status === 'failed')     failed++;
      }
    }
    return { frames, generating, pending, approved, failed, upcoming };
  }, [data]);

  return (
    <div className="h-full flex flex-col p-5 gap-4 overflow-hidden">
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="text-[15px] font-semibold text-text">Queue</h2>
          <p className="text-[11px] text-text-dim mt-0.5">
            {data.length} pipeline{data.length === 1 ? '' : 's'} ·
            {' '}{totals.frames} frame{totals.frames === 1 ? '' : 's'} ·
            {' '}{totals.upcoming} upcoming ·
            {' '}<span style={{ color: '#a78bfa' }}>{totals.generating} generating</span> ·
            {' '}<span style={{ color: '#fbbf24' }}>{totals.pending} pending review</span> ·
            {' '}<span style={{ color: '#10b981' }}>{totals.approved} approved</span>
            {totals.failed > 0 && <> · <span style={{ color: '#ef4444' }}>{totals.failed} failed</span></>}
          </p>
        </div>
        <button onClick={fetchQueue} disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] text-text-dim hover:text-text border border-border-soft hover:border-border transition-colors">
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {err && (
        <div className="text-[12px] text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
          {err}
        </div>
      )}

      {!loading && !err && data.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center text-text-dim text-[13px] gap-2">
          <ListChecks size={32} strokeWidth={1.5} />
          <div>No live pipelines. Start one from the All Pipelines tab.</div>
        </div>
      )}

      <div className="flex-1 overflow-y-auto flex flex-col gap-3 pr-1">
        {data.map((p) => (
          <div key={p.id}
               style={{ borderRadius: 8,
                         border: '1px solid rgba(255,255,255,0.08)',
                         background: 'rgba(255,255,255,0.02)' }}>
            {/* Pipeline header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10,
                           padding: '10px 14px',
                           borderBottom: '1px solid rgba(255,255,255,0.05)',
                           background: 'rgba(0,229,255,0.04)' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
                  {p.name}
                </div>
                <div style={{ fontSize: 10, color: '#7f8aa3', fontFamily: 'JetBrains Mono, monospace', marginTop: 2 }}>
                  {p.id} · stage={p.stage}{p.paused ? ' · paused' : ''}{p.cancelled ? ' · cancelled' : ''}
                </div>
              </div>
              <span style={{ fontSize: 10, color: '#7f8aa3' }}>
                {p.frame_count} frame{p.frame_count === 1 ? '' : 's'} · {p.upcoming_count} upcoming
              </span>
            </div>

            {/* Frames table */}
            {p.frames.length > 0 && (
              <div style={{ padding: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase',
                               letterSpacing: '0.1em', padding: '0 8px 4px', fontWeight: 600 }}>
                  Frames in flight ({p.frames.length})
                </div>
                {p.frames.map((f) => {
                  const c = statusColor(f.status);
                  return (
                    <div key={f.id}
                         style={{ display: 'flex', alignItems: 'center', gap: 8,
                                   padding: '6px 10px', borderRadius: 4,
                                   background: '#0a0f1c',
                                   borderLeft: `3px solid ${c}`,
                                   fontSize: 11 }}>
                      <span style={{ fontFamily: 'JetBrains Mono, monospace',
                                      fontSize: 10, color: '#7f8aa3', minWidth: 60 }}>
                        {f.section || '?'}{f.slide_idx != null ? `·${f.slide_idx + 1}` : ''}
                      </span>
                      <span style={{ flex: 1, minWidth: 0,
                                      color: '#dbe3f0', overflow: 'hidden',
                                      textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                            title={f.prompt_text}>
                        {f.prompt_text}
                      </span>
                      {f.retry_count != null && f.retry_count > 0 && (
                        <span style={{ fontSize: 9, color: '#fbbf24',
                                        background: 'rgba(251,191,36,0.12)',
                                        padding: '1px 5px', borderRadius: 2 }}>
                          retry {f.retry_count}
                        </span>
                      )}
                      <span style={{ fontSize: 10, fontWeight: 700, color: c,
                                      letterSpacing: '0.05em', textTransform: 'uppercase',
                                      minWidth: 90, textAlign: 'right' }}>
                        {f.status}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Upcoming approved prompts */}
            {p.upcoming.length > 0 && (
              <div style={{ padding: 8, borderTop: p.frames.length > 0 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                             display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase',
                               letterSpacing: '0.1em', padding: '0 8px 4px', fontWeight: 600 }}>
                  Upcoming ({p.upcoming.length} approved · awaiting render)
                </div>
                {p.upcoming.slice(0, 12).map((u) => (
                  <div key={u.id}
                       style={{ display: 'flex', alignItems: 'center', gap: 8,
                                 padding: '5px 10px', borderRadius: 4,
                                 background: '#0a0f1c',
                                 borderLeft: '3px dashed #fbbf24',
                                 fontSize: 11, opacity: 0.85 }}>
                    <span style={{ fontFamily: 'JetBrains Mono, monospace',
                                    fontSize: 10, color: '#fbbf24', minWidth: 60 }}>
                      {u.topic_number != null ? `T${u.topic_number}` : '·'}
                      {u.slide_number != null ? `·S${String(u.slide_number).padStart(2, '0')}` : ''}
                    </span>
                    <span style={{ flex: 1, minWidth: 0,
                                    color: '#a5b4c9', overflow: 'hidden',
                                    textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                          title={u.text}>
                      {u.text}
                    </span>
                    <span style={{ fontSize: 10, fontWeight: 700, color: '#fbbf24',
                                    letterSpacing: '0.05em', textTransform: 'uppercase' }}>
                      queued
                    </span>
                  </div>
                ))}
                {p.upcoming.length > 12 && (
                  <div style={{ fontSize: 10, color: '#53607a', padding: '4px 10px' }}>
                    +{p.upcoming.length - 12} more queued
                  </div>
                )}
              </div>
            )}

            {p.frames.length === 0 && p.upcoming.length === 0 && (
              <div style={{ padding: '14px', fontSize: 11, color: '#53607a',
                             fontStyle: 'italic', textAlign: 'center' }}>
                Nothing in this pipeline's queue yet — load a source MD and run a batch.
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function StatChip({ icon: Icon, label, value, tone }: {
  icon: any; label: string; value: number; tone: 'cyan'|'violet'|'green';
}) {
  const colors = {
    cyan:   { bg: 'rgba(0,229,255,0.06)', bd: 'rgba(0,229,255,0.18)', fg: '#00e5ff', glow: 'rgba(0,229,255,0.25)' },
    violet: { bg: 'rgba(168,85,247,0.06)', bd: 'rgba(168,85,247,0.18)', fg: '#c4b5fd', glow: 'rgba(168,85,247,0.25)' },
    green:  { bg: 'rgba(16,185,129,0.06)', bd: 'rgba(16,185,129,0.18)', fg: '#10b981', glow: 'rgba(16,185,129,0.25)' },
  }[tone];
  return (
    <motion.div
      whileHover={{ y: -2, transition: { duration: 0.12 } }}
      className="rounded-xl px-3 py-3 border relative overflow-hidden group cursor-default"
      style={{background: colors.bg, borderColor: colors.bd, backdropFilter: 'blur(8px)'}}>
      {/* subtle glow on hover */}
      <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none"
        style={{background: `radial-gradient(circle at 50% 0%, ${colors.glow} 0%, transparent 70%)`}} />
      <div className="flex items-center gap-1 mb-1 relative" style={{color: colors.fg}}>
        <Icon size={10} strokeWidth={2.5} />
        <span className="text-[9px] uppercase tracking-wider font-semibold opacity-80">{label}</span>
      </div>
      <div className="text-[20px] font-bold leading-none relative" style={{color: colors.fg, textShadow: `0 0 20px ${colors.glow}`}}>{value}</div>
    </motion.div>
  );
}

function MainDashboard({ config, pipeline, activity, onSendCommand, onDirectAction, onPreviewImage, onOpenSettings, voiceState, lastReply }: {
  config: LotusConfig | null;
  pipeline: PipelineState | null;
  activity: ActivityItem[];
  onSendCommand: (text: string) => void;
  onDirectAction: (action: string, payload?: Record<string, any>) => void;
  onPreviewImage: (img: { url: string; name: string; section: string; folder: string }) => void;
  onOpenSettings: () => void;
  voiceState: 'idle' | 'listening' | 'thinking' | 'speaking';
  lastReply: string;
}) {
  const [today, setToday] = useState<{ folder: string; images: any[] }>({ folder: '', images: [] });
  const refreshToday = useCallback(() => {
    fetch('/api/today').then(r => r.json()).then(setToday).catch(() => {});
  }, []);
  useEffect(() => {
    refreshToday();
    const t = setInterval(refreshToday, 8000);
    // Gemma can push a `today_refresh` event after show_today; activity array
    // grows on every push, so watching its latest entry is a cheap trigger.
    return () => clearInterval(t);
  }, [refreshToday]);
  useEffect(() => {
    // Any activity change from a "reply" or "stage" event often implies new
    // files on disk — cheap nudge beats polling delay.
    if (activity[0] && (activity[0].kind === 'reply' || activity[0].kind === 'stage')) {
      refreshToday();
    }
  }, [activity, refreshToday]);

  const card = (title: string, color: string, children: ReactNode) => (
    <div style={{ background: '#141a28', border: '1px solid #232d45', borderRadius: 12, padding: 18 }}>
      <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.25em',
                    fontWeight: 700, color, marginBottom: 12 }}>{title}</div>
      {children}
    </div>
  );

  // Tiles data — rendered into a 2-column scrollable list (new layout).
  const tiles: any[] = [
    { label: 'Start Pipeline', cmd: 'start pipeline Demo', icon: '⚡', color: '#ff6b35',
      hint: 'start_pipeline' },
    { label: 'Show Today',     direct: 'show_today',        icon: '📅', color: '#00e5ff',
      hint: 'show_today' },
    { label: 'Review Posts',   direct: 'review_posts',      icon: '📝', color: '#ff6b35',
      hint: 'review_posts' },
    { label: 'Generate Image', icon: '🎨', color: '#a78bfa',
      hint: 'create_image', ask: 'What image should Gemma generate?',
      template: (v: string) => `generate an image of ${v}` },
    { label: 'Latest News',    direct: 'get_news', directPayload: { topic: 'trending' },
      icon: '📰', color: '#fbbf24', hint: 'get_news' },
    { label: 'Ask Claude',     icon: '🧠', color: '#e879f9',
      hint: 'ask_claude',
      ask: 'What should I ask Claude? (research, reel ideas, anything deep)',
      template: (v: string) => v },
    { label: 'Open Projects',  direct: 'open_projects_folder',
      icon: '📂', color: '#10b981', hint: 'open_projects_folder' },
  ];
  const runTile = (q: any) => {
    if (q.ask) {
      const p = window.prompt(q.ask, '');
      if (!p || !p.trim()) return;
      onSendCommand(q.template(p.trim()));
    } else if (q.direct) {
      onDirectAction(q.direct, q.directPayload || {});
    } else if (q.cmd) {
      onSendCommand(q.cmd);
    }
  };

  return (
    <div className="h-full overflow-hidden flex flex-col">
      {/* Narrow top strip — only LOTUS + GEMMA animations. */}
      <VoiceStripTop state={voiceState} lastReply={lastReply} />

      {/* Main body: LEFT = actions (2-col scroll), RIGHT = everything else
          stacked vertically in a scroll column. */}
      <div className="flex-1 min-h-0 grid gap-4 px-5 py-4"
           style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(320px, 0.85fr)' }}>

        {/* LEFT — Quick action tiles, 2 columns, scrollable */}
        <section className="overflow-auto" style={{
          borderRadius: 14, background: 'rgba(20,26,40,0.45)',
          border: '1px solid #232d45', padding: 14,
        }}>
          <div className="flex items-center justify-between mb-3 px-1">
            <div style={{ fontSize: 10, fontWeight: 800, color: '#ff6b35',
                          letterSpacing: '0.3em', textTransform: 'uppercase' }}>
              Quick Actions
            </div>
            <div style={{ fontSize: 10, color: '#7f8aa3', letterSpacing: '0.2em',
                          textTransform: 'uppercase' }}>
              {tiles.length} tiles
            </div>
          </div>
          <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            {tiles.map((q) => (
              <button key={q.label} onClick={() => runTile(q)}
                style={{
                  background: '#141a28', border: '1px solid #232d45', borderRadius: 10,
                  padding: '14px 12px', color: '#e6ebf5', cursor: 'pointer',
                  transition: 'all 0.15s', textAlign: 'left', minHeight: 88,
                }}
                onMouseEnter={(e) => { e.currentTarget.style.borderColor = q.color;
                                       e.currentTarget.style.transform = 'translateY(-2px)'; }}
                onMouseLeave={(e) => { e.currentTarget.style.borderColor = '#232d45';
                                       e.currentTarget.style.transform = 'none'; }}>
                <div style={{ fontSize: 22, marginBottom: 6, color: q.color }}>{q.icon}</div>
                <div style={{ fontSize: 12.5, fontWeight: 700 }}>{q.label}</div>
                <div style={{ fontSize: 10, color: '#7f8aa3', marginTop: 3,
                              fontFamily: 'JetBrains Mono, monospace' }}>
                  › {q.hint}()
                </div>
              </button>
            ))}
          </div>
        </section>

        {/* RIGHT — the rest, stacked vertically and scrollable. */}
        <section className="overflow-auto" style={{
          borderRadius: 14, background: 'rgba(20,26,40,0.3)',
          border: '1px solid #232d45', padding: 14,
        }}>
        <div className="space-y-4">
        {/* Welcome header (compact) */}
        <div className="text-center pb-1">
          <div className="font-display font-black text-[34px] tracking-[10px] text-text leading-none">
            LOTUS
          </div>
          <div className="text-[10px] tracking-[6px] text-text-dim mt-1.5">AGENT · CONTROL CENTER</div>
          <div className="text-[11.5px] text-text-dim mt-2">
            Say <code className="text-accent bg-accent/10 px-1.5 py-0.5 rounded">"Lotus, start pipeline"</code>
          </div>
        </div>

        {/* Active pipeline banner (if any) */}
        {pipeline && (
          <div style={{
            background: 'linear-gradient(90deg, rgba(255,107,53,0.12), rgba(0,229,255,0.08))',
            border: '1px solid rgba(255,107,53,0.35)', borderRadius: 12, padding: 16,
            display: 'flex', alignItems: 'center', gap: 16,
          }}>
            <span className="animate-pulse-soft" style={{ fontSize: 28 }}>⚡</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
                Active pipeline: <span style={{ color: '#ff6b35' }}>{pipeline.name}</span>
              </div>
              <div style={{ fontSize: 11, color: '#7f8aa3', marginTop: 2 }}>
                Stage <span style={{ color: '#fbbf24', fontFamily: 'JetBrains Mono, monospace' }}>{pipeline.stage}</span>
                {' · '}{pipeline.frames.length} frames · {pipeline.prompts.length} prompts
              </div>
            </div>
            <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.2em',
                          color: '#ff6b35', fontWeight: 700 }}>
              Switch to Pipeline tab →
            </div>
          </div>
        )}

        {/* Vertical stack: rules, today's output, activity — one per row. */}
        <div className="flex flex-col gap-3">
          {card('Naming Rules', '#00e5ff', (
            config ? (
              <>
                <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10.5,
                              color: '#e6ebf5', background: 'rgba(0,229,255,0.05)',
                              padding: '8px 10px', borderRadius: 6, marginBottom: 8,
                              border: '1px solid rgba(0,229,255,0.15)',
                              wordBreak: 'break-all', lineHeight: 1.5 }}>
                  <span style={{ color: '#7f8aa3' }}>Root:</span>{' '}
                  <span style={{ color: '#00e5ff' }}>{config.projects_root}</span>
                </div>
                <button onClick={onOpenSettings} style={{
                  width: '100%', marginBottom: 10, padding: '6px 10px',
                  fontSize: 10, fontWeight: 700, borderRadius: 6,
                  background: 'rgba(0,229,255,0.12)', color: '#00e5ff',
                  border: '1px solid rgba(0,229,255,0.35)', cursor: 'pointer',
                  textTransform: 'uppercase', letterSpacing: '0.12em',
                }}>📁 Change projects location</button>
                <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#e6ebf5',
                              background: 'rgba(0,229,255,0.05)', padding: '10px 12px', borderRadius: 6,
                              marginBottom: 10, border: '1px solid rgba(0,229,255,0.15)', lineHeight: 1.7 }}>
                  {config.parent_folder}/<br/>
                  ├── Post1/Frame1{config.frame_ext}<br/>
                  ├── Reel/Frame1{config.frame_ext}<br/>
                  ├── ReelCover{config.frame_ext}<br/>
                  └── Story1{config.frame_ext}
                </div>
                <div className="flex flex-wrap gap-1 mb-2">
                  {config.multi_frame_sections.map(s => (
                    <span key={s} style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 999,
                      background: 'rgba(167,139,250,0.12)', color: '#a78bfa',
                      border: '1px solid rgba(167,139,250,0.25)',
                      fontFamily: 'JetBrains Mono, monospace',
                    }}>{s}/</span>
                  ))}
                </div>
                <div className="flex flex-wrap gap-1">
                  {config.single_item_slots.map(s => (
                    <span key={s} style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 999,
                      background: 'rgba(255,107,53,0.12)', color: '#ff6b35',
                      border: '1px solid rgba(255,107,53,0.25)',
                      fontFamily: 'JetBrains Mono, monospace',
                    }}>{s}</span>
                  ))}
                </div>
                <div style={{ fontSize: 10.5, color: '#7f8aa3', marginTop: 10 }}>
                  Parent: <code style={{ color: '#e6ebf5' }}>{config.brand}&lt;MMDDYYYY&gt;</code>
                  {' · '}frame numbering resumes automatically.
                </div>

                {/* Image-engine switch — flips primary tier in _gen_image
                    live (no agent restart). Falls back automatically if
                    the chosen engine errors. */}
                <div style={{
                  marginTop: 14, padding: '10px 12px', borderRadius: 6,
                  background: 'rgba(167,139,250,0.06)',
                  border: '1px solid rgba(167,139,250,0.2)',
                }}>
                  <div style={{
                    fontSize: 10, fontWeight: 700, color: '#a78bfa',
                    textTransform: 'uppercase', letterSpacing: '0.12em',
                    marginBottom: 6, display: 'flex',
                    alignItems: 'center', gap: 6,
                  }}>
                    <span>🧠 Image engine</span>
                    <span style={{
                      fontSize: 9, padding: '1px 6px', borderRadius: 999,
                      background: 'rgba(167,139,250,0.2)', color: '#c4b5fd',
                      letterSpacing: '0.08em',
                    }}>{config.pipeline || 'playwright'}</span>
                  </div>
                  <select
                    value={config.pipeline || 'playwright'}
                    onChange={(e) => {
                      const val = e.target.value;
                      fetch('/api/config/pipeline', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ pipeline: val }),
                      })
                        .then(r => r.json())
                        .then(j => {
                          if (j.error) {
                            console.error('pipeline switch failed:', j.error);
                          }
                          // The agent broadcasts config_updated with the new
                          // pipeline value, which our WS handler picks up
                          // and refetches /api/config — no manual refresh.
                        })
                        .catch(err => console.error('pipeline switch error:', err));
                    }}
                    style={{
                      width: '100%', padding: '6px 8px',
                      fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
                      borderRadius: 4, color: '#e6ebf5',
                      background: 'rgba(0,0,0,0.3)',
                      border: '1px solid rgba(167,139,250,0.3)',
                      cursor: 'pointer',
                    }}
                  >
                    <option value="playwright">Playwright — gemini_bot.py (default)</option>
                    <option value="human">Human-input — gemini_bot_human.py (OS-level)</option>
                    <option value="api">Official API — gemini_api.py (needs key)</option>
                  </select>
                  <div style={{
                    fontSize: 9.5, color: '#7f8aa3', marginTop: 6, lineHeight: 1.5,
                  }}>
                    Auto-falls-back through the chain if the chosen engine errors.
                    No restart needed — change takes effect on the next image gen.
                  </div>
                </div>
              </>
            ) : <div className="text-text-dim text-[11px] italic">Loading config…</div>
          ))}

          {card(`Today's Output · ${today.images.length}`, '#10b981', (
            <div>
              <div style={{
                background: 'rgba(16,185,129,0.05)',
                border: '1px solid rgba(16,185,129,0.2)',
                borderRadius: 8, padding: '8px 10px', marginBottom: 10,
                fontSize: 11, color: '#e6ebf5',
                fontFamily: 'JetBrains Mono, monospace',
                display: 'flex', alignItems: 'center', gap: 6,
              }}>
                <span style={{ color: '#10b981' }}>📁</span>
                <span style={{ wordBreak: 'break-all' }}>
                  {today.folder || '(no output yet)'}/
                </span>
              </div>
              {(() => {
                const bySection: Record<string, any[]> = {};
                (today.images || []).forEach(i => {
                  const k = i.section || '(root)';
                  (bySection[k] = bySection[k] || []).push(i);
                });
                const sections = Object.keys(bySection).sort();
                if (sections.length === 0) {
                  return <div className="text-text-dim text-[11px] italic">No frames saved today. Generated images land under <code className="text-green bg-green/10 px-1.5 py-0.5 rounded">&lt;parent&gt;/&lt;Section&gt;/FrameN.png</code>.</div>;
                }
                return sections.map(sec => (
                  <div key={sec} style={{ marginBottom: 10 }}>
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa',
                                  letterSpacing: '0.2em', textTransform: 'uppercase',
                                  marginBottom: 5, display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span>└── {sec}/</span>
                      <span style={{ color: '#7f8aa3', fontWeight: 500, letterSpacing: 0 }}>
                        {bySection[sec].length} file{bySection[sec].length>1?'s':''}
                      </span>
                    </div>
                    <div className="grid grid-cols-3 gap-1.5">
                      {bySection[sec].slice(0, 6).map((img: any) => (
                        <button key={img.url}
                          onClick={() => onPreviewImage({
                            url: img.url, name: img.name,
                            section: img.section || sec,
                            folder: today.folder || '',
                          })}
                          style={{ aspectRatio: '1', borderRadius: 6, overflow: 'hidden',
                                   border: '1px solid #232d45', position: 'relative',
                                   display: 'block', padding: 0, cursor: 'pointer',
                                   background: 'transparent' }}
                          onMouseEnter={(e) => e.currentTarget.style.borderColor = '#10b981'}
                          onMouseLeave={(e) => e.currentTarget.style.borderColor = '#232d45'}>
                          <img src={`${img.url}?cb=${Date.now()}`} alt={img.name} style={{
                            width: '100%', height: '100%', objectFit: 'cover',
                          }}/>
                          <div style={{
                            position: 'absolute', bottom: 0, left: 0, right: 0,
                            background: 'linear-gradient(transparent, rgba(0,0,0,0.9))',
                            padding: '12px 4px 3px', fontSize: 9, color: '#fff',
                            fontFamily: 'JetBrains Mono, monospace', textAlign: 'center',
                          }}>{img.name}</div>
                        </button>
                      ))}
                    </div>
                  </div>
                ));
              })()}
            </div>
          ))}

          {card('Recent Activity', '#a78bfa', (
            <div style={{ maxHeight: 280, overflow: 'auto' }}>
              {activity.length === 0
                ? <div className="text-text-dim text-[11px] italic">No events yet.</div>
                : activity.slice(0, 12).map((a) => {
                    const c = a.kind === 'cmd' ? '#ff6b35'
                            : a.kind === 'reply' ? '#00e5ff'
                            : a.kind === 'stage' ? '#fbbf24'
                            : '#7f8aa3';
                    const t = new Date(a.ts).toLocaleTimeString('en-US', { hour12: false });
                    return (
                      <div key={a.id} style={{ padding: '6px 0', borderBottom: '1px solid rgba(35,45,69,0.5)' }}>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 2 }}>
                          <span style={{ color: '#4e5872', fontFamily: 'JetBrains Mono, monospace', fontSize: 9.5 }}>{t}</span>
                          <span style={{ color: c, textTransform: 'uppercase', fontWeight: 700, fontSize: 9.5, letterSpacing: '0.1em' }}>
                            {a.kind}
                          </span>
                        </div>
                        <div className="text-text-dim truncate" style={{ fontSize: 11 }}>{a.text}</div>
                      </div>
                    );
                  })}
            </div>
          ))}
        </div>
        </div>
        </section>
      </div>
    </div>
  );
}

function GeminiKeysPanel({ status, test, onAdd, onRemove, onRefresh, onTest, onClearTest }: {
  status: any;
  test: any;
  onAdd: (key: string) => void;
  onRemove: (preview: string) => void;
  onRefresh: () => void;
  onTest: (key?: string) => void;
  onClearTest: () => void;
}) {
  const [newKey, setNewKey] = useState('');
  const previews: string[] = (status?.key_previews) || [];
  const nKeys = Number(status?.api_keys_found || 0);
  const nActive = Number(status?.keys_active || 0);
  const sdkOk = !!status?.sdk_installed;
  const model = status?.model || 'gemini-3.1-flash-image-preview';

  const chip = (label: string, color: string, hint?: string) => (
    <span style={{
      fontSize: 10, padding: '3px 9px', borderRadius: 999,
      background: `${color}22`, color, border: `1px solid ${color}55`,
      fontWeight: 700, letterSpacing: '0.15em', textTransform: 'uppercase',
    }} title={hint}>{label}</span>
  );

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <span style={{ fontSize: 10, fontWeight: 800, color: '#a78bfa',
                       letterSpacing: '0.25em', textTransform: 'uppercase' }}>
          🧠 Gemini API keys (NB2)
        </span>
        <span style={{ flex: 1 }} />
        {chip(sdkOk ? 'SDK OK' : 'SDK MISSING', sdkOk ? '#10b981' : '#ef4444')}
        {chip(`${nActive}/${nKeys} ACTIVE`, nKeys > 0 ? '#10b981' : '#7f8aa3')}
      </div>

      {/* Status banner */}
      {!sdkOk && (
        <div style={{ padding: '10px 12px', borderRadius: 8, marginBottom: 10,
                      background: 'rgba(239,68,68,0.1)',
                      border: '1px solid rgba(239,68,68,0.3)', fontSize: 11, color: '#fca5a5' }}>
          ⚠ google-genai SDK not installed. Run in terminal:{' '}
          <code style={{ fontFamily: 'JetBrains Mono, monospace' }}>
            source ~/LotusAgent/venv/bin/activate && pip install google-genai
          </code>
        </div>
      )}
      <div style={{ fontSize: 10.5, color: '#7f8aa3', marginBottom: 10 }}>
        Model: <code style={{ color: '#a78bfa' }}>{model}</code>{' '}
        · 3 Pro keys = 3× daily quota + auto-failover on 429.
      </div>

      {/* Existing keys list */}
      <div style={{ fontSize: 10, color: '#fbbf24', textTransform: 'uppercase',
                    letterSpacing: '0.22em', fontWeight: 700, marginBottom: 6 }}>
        Configured keys ({nKeys})
      </div>
      <div style={{ marginBottom: 12 }}>
        {previews.length === 0 && (
          <div style={{ padding: '10px 12px', borderRadius: 8,
                        background: '#0a0f1c', border: '1px solid #232d45',
                        fontSize: 11, color: '#7f8aa3', fontStyle: 'italic' }}>
            No keys yet. Add your first below.
          </div>
        )}
        {previews.map((p, i) => (
          <div key={i} style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
            borderRadius: 8, background: '#0a0f1c', border: '1px solid #232d45',
            marginBottom: 4,
          }}>
            <span style={{ fontSize: 10, color: '#7f8aa3', fontFamily: 'JetBrains Mono, monospace',
                           width: 22 }}>#{i + 1}</span>
            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12,
                           color: '#e6ebf5', flex: 1 }}>{p}</span>
            <button onClick={() => onTest(p)}
              title="Fire a cheap text test to confirm the key works"
              style={{
                fontSize: 9.5, padding: '3px 8px', borderRadius: 4,
                background: 'rgba(0,229,255,0.12)', color: '#00e5ff',
                border: '1px solid rgba(0,229,255,0.3)', cursor: 'pointer',
                textTransform: 'uppercase', letterSpacing: '0.12em', fontWeight: 700,
              }}>Test</button>
            <button onClick={() => {
              if (window.confirm(`Remove key ${p}?`)) onRemove(p);
            }}
              style={{
                fontSize: 9.5, padding: '3px 8px', borderRadius: 4,
                background: 'rgba(239,68,68,0.12)', color: '#ef4444',
                border: '1px solid rgba(239,68,68,0.3)', cursor: 'pointer',
                textTransform: 'uppercase', letterSpacing: '0.12em', fontWeight: 700,
              }}>✗</button>
          </div>
        ))}
      </div>

      {/* Test result banner */}
      {test && (
        <div style={{
          padding: '10px 12px', borderRadius: 8, marginBottom: 10,
          background: test.ok ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)',
          border: `1px solid ${test.ok ? 'rgba(16,185,129,0.35)' : 'rgba(239,68,68,0.35)'}`,
          fontSize: 11, color: '#e6ebf5',
          display: 'flex', gap: 8, alignItems: 'flex-start',
        }}>
          <span style={{ color: test.ok ? '#10b981' : '#ef4444', fontSize: 14 }}>
            {test.ok ? '✓' : '✗'}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 700, color: test.ok ? '#10b981' : '#ef4444', marginBottom: 3 }}>
              {test.ok ? `Key OK — ${test.preview}` : `Key FAILED — ${test.preview}`}
            </div>
            <div style={{ fontSize: 10.5, color: '#7f8aa3' }}>
              {test.ok
                ? `model=${test.model} reply="${(test.reply_preview||'').slice(0, 80)}"`
                : (test.error || '(no error detail)').slice(0, 200)}
            </div>
          </div>
          <button onClick={onClearTest} style={{
            background: 'transparent', border: 'none', color: '#7f8aa3',
            fontSize: 14, cursor: 'pointer', padding: 0,
          }}>✕</button>
        </div>
      )}

      {/* Add new key */}
      <div style={{ fontSize: 10, color: '#10b981', textTransform: 'uppercase',
                    letterSpacing: '0.22em', fontWeight: 700, marginBottom: 6 }}>
        Add key
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        <input
          type="password" value={newKey} onChange={(e) => setNewKey(e.target.value)}
          placeholder="AIzaSy… (paste from aistudio.google.com/apikey)"
          style={{
            flex: 1, background: '#0a0f1c', border: '1px solid #232d45',
            borderRadius: 8, padding: '9px 12px', color: '#e6ebf5',
            fontSize: 12, fontFamily: 'JetBrains Mono, monospace', outline: 'none',
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && newKey.trim().length > 20) {
              onAdd(newKey.trim()); setNewKey('');
            }
          }}
        />
        <button disabled={newKey.trim().length < 20}
          onClick={() => { onAdd(newKey.trim()); setNewKey(''); }}
          style={{
            fontSize: 11, fontWeight: 800, padding: '9px 16px', borderRadius: 8,
            background: newKey.trim().length < 20 ? 'rgba(16,185,129,0.08)' : 'rgba(16,185,129,0.22)',
            color: newKey.trim().length < 20 ? '#4e5872' : '#10b981',
            border: `1px solid ${newKey.trim().length < 20 ? '#232d45' : 'rgba(16,185,129,0.45)'}`,
            cursor: newKey.trim().length < 20 ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>＋ Add</button>
      </div>
      <div style={{ fontSize: 10.5, color: '#7f8aa3', marginTop: 8, lineHeight: 1.5 }}>
        Get keys free at{' '}
        <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer"
           style={{ color: '#00e5ff' }}>aistudio.google.com/apikey</a>.
        Stored in <code style={{ color: '#e6ebf5' }}>~/.lotus_auth/gemini_api_key.txt</code>{' '}
        (chmod 600). The Test button fires a free text-only ping to confirm.
      </div>
      <div style={{ marginTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={onRefresh} style={{
          fontSize: 10, fontWeight: 700, padding: '5px 10px', borderRadius: 6,
          background: 'transparent', color: '#7f8aa3',
          border: '1px solid #232d45', cursor: 'pointer',
          textTransform: 'uppercase', letterSpacing: '0.12em',
        }}>↻ Refresh</button>
      </div>
    </div>
  );
}

function ProjectsRootSettings({ config, onClose, onSave, onOpenFolder, gemini }: {
  config: LotusConfig | null;
  onClose: () => void;
  onSave: (path: string) => void;
  onOpenFolder: () => void;
  gemini: React.ReactNode;
}) {
  const current = config?.projects_root || '';
  // Pull $HOME out of the current projects_root. Handle both expanded
  // (/Users/rasoindia/...) and tilde-prefixed (~/LotusAgent/...) forms —
  // falling through the first match yields a real path, not /Users/me.
  const home = current.startsWith('/Users/')
    ? current.match(/^\/Users\/[^/]+/)?.[0] || ''
    : (current.startsWith('/home/') ? current.match(/^\/home\/[^/]+/)?.[0] || '' : '');
  const presets = [
    { label: 'Desktop',      path: `${home}/Desktop/LOTUS Projects` },
    { label: 'Documents',    path: `${home}/Documents/LOTUS Projects` },
    { label: 'Downloads',    path: `${home}/Downloads/LOTUS Projects` },
    { label: 'Home',         path: `${home}/LOTUS Projects` },
    { label: 'Default',      path: `${home}/LotusAgent/Projects` },
  ];
  const [custom, setCustom] = useState(current);
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(5,8,16,0.82)',
      zIndex: 60, display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 640, maxWidth: 'calc(100vw - 40px)',
        background: '#141a28', border: '1px solid rgba(0,229,255,0.5)', borderRadius: 14,
        boxShadow: '0 18px 60px rgba(0,0,0,0.7), 0 0 0 1px rgba(0,229,255,0.25)',
        overflow: 'hidden',
      }}>
        <div style={{ padding: '14px 18px', borderBottom: '1px solid #232d45',
                      display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{
            width: 36, height: 36, borderRadius: 9,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 18, background: 'rgba(0,229,255,0.18)', color: '#00e5ff',
          }}>📁</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
              Projects Root — where parent folders are created
            </div>
            <div style={{ fontSize: 10, color: '#7f8aa3', letterSpacing: '0.2em',
                          textTransform: 'uppercase', marginTop: 2 }}>
              Pick a location or paste a full path
            </div>
          </div>
          <button onClick={onClose} style={{
            background: 'transparent', border: 'none', color: '#7f8aa3',
            fontSize: 18, cursor: 'pointer', padding: 4,
          }}>✕</button>
        </div>

        <div style={{ padding: 16 }}>
          <div style={{
            padding: '10px 12px', borderRadius: 8,
            background: 'rgba(0,229,255,0.08)',
            border: '1px solid rgba(0,229,255,0.3)', marginBottom: 14,
          }}>
            <div style={{ fontSize: 9.5, color: '#7f8aa3', textTransform: 'uppercase',
                          letterSpacing: '0.22em', fontWeight: 700, marginBottom: 4 }}>
              Current location
            </div>
            <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12,
                          color: '#e6ebf5', wordBreak: 'break-all', lineHeight: 1.55 }}>
              {current || '(unknown)'}
            </div>
            {config && (
              <div style={{ fontSize: 11, color: '#7f8aa3', marginTop: 6 }}>
                Today's parent: <code style={{ color: '#ff6b35' }}>{config.parent_folder}/</code>
              </div>
            )}
            <button onClick={onOpenFolder} style={{
              marginTop: 8, fontSize: 10, fontWeight: 700, padding: '5px 10px',
              borderRadius: 6, background: 'rgba(16,185,129,0.2)', color: '#10b981',
              border: '1px solid rgba(16,185,129,0.4)', cursor: 'pointer',
              textTransform: 'uppercase', letterSpacing: '0.12em',
            }}>📂 Open in Finder</button>
          </div>

          <div style={{ fontSize: 10, color: '#a78bfa', textTransform: 'uppercase',
                        letterSpacing: '0.22em', fontWeight: 700, marginBottom: 8 }}>
            Quick picks
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 6,
                        marginBottom: 14 }}>
            {presets.map(p => (
              <button key={p.label} onClick={() => setCustom(p.path)}
                style={{
                  padding: '10px 12px', textAlign: 'left', borderRadius: 8,
                  background: custom === p.path ? 'rgba(255,107,53,0.16)' : '#0a0f1c',
                  border: `1px solid ${custom === p.path ? 'rgba(255,107,53,0.5)' : '#232d45'}`,
                  color: '#e6ebf5', cursor: 'pointer',
                }}
                onMouseEnter={(e) => { if (custom !== p.path) e.currentTarget.style.borderColor = '#a78bfa'; }}
                onMouseLeave={(e) => { if (custom !== p.path) e.currentTarget.style.borderColor = '#232d45'; }}>
                <div style={{ fontSize: 12, fontWeight: 700 }}>{p.label}</div>
                <div style={{ fontSize: 10, color: '#7f8aa3', fontFamily: 'JetBrains Mono, monospace',
                              marginTop: 3, wordBreak: 'break-all' }}>{p.path}</div>
              </button>
            ))}
          </div>

          <div style={{ fontSize: 10, color: '#fbbf24', textTransform: 'uppercase',
                        letterSpacing: '0.22em', fontWeight: 700, marginBottom: 8 }}>
            Custom path
          </div>
          <input
            type="text" value={custom} onChange={(e) => setCustom(e.target.value)}
            placeholder="/Users/you/path/to/Projects"
            style={{
              width: '100%', background: '#0a0f1c', border: '1px solid #232d45',
              borderRadius: 8, padding: '10px 12px', color: '#e6ebf5',
              fontSize: 12, fontFamily: 'JetBrains Mono, monospace', outline: 'none',
            }}
          />
          <div style={{ fontSize: 10.5, color: '#7f8aa3', marginTop: 10, lineHeight: 1.5 }}>
            The parent folder (e.g. <code style={{ color: '#e6ebf5' }}>Techengine04192026</code>)
            is auto-created inside this root on the first generation. If the folder already
            contains frames, numbering resumes from the next available.
          </div>
        </div>

        <div style={{ padding: 12, borderTop: '1px solid #232d45',
                      display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onClose} style={{
            fontSize: 11, fontWeight: 700, padding: '9px 16px', borderRadius: 8,
            background: 'transparent', color: '#7f8aa3',
            border: '1px solid #232d45', cursor: 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>Close</button>
          <button disabled={!custom.trim() || custom === current} onClick={() => onSave(custom.trim())}
            style={{
              fontSize: 11, fontWeight: 800, padding: '9px 18px', borderRadius: 8,
              background: (!custom.trim() || custom === current) ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.22)',
              color: (!custom.trim() || custom === current) ? '#4e5872' : '#10b981',
              border: `1px solid ${(!custom.trim() || custom === current) ? '#232d45' : 'rgba(16,185,129,0.5)'}`,
              cursor: (!custom.trim() || custom === current) ? 'not-allowed' : 'pointer',
              textTransform: 'uppercase', letterSpacing: '0.1em',
            }}>✓ Save location</button>
        </div>

        {/* Gemini API keys panel — second section in the same modal. */}
        <div style={{ borderTop: '2px solid #232d45', background: 'rgba(167,139,250,0.03)' }}>
          {gemini}
        </div>
      </div>
    </div>
  );
}

function PostImageGenOverlay({ gen, onClose, onOpenFolder, onRegenerate, onNextPost, onApprove, viewOnly }: {
  gen: PostImageGen;
  onClose: () => void;
  onOpenFolder: () => void;
  onRegenerate: () => void;
  onNextPost: () => void;
  onApprove: () => void;
  viewOnly?: boolean;
}) {
  const isWorking = gen.status === 'working';
  const isDone    = gen.status === 'done';
  const isError   = gen.status === 'error';
  const accent = isWorking ? '#fbbf24'
              : isDone    ? '#10b981'
              : '#ef4444';
  const url    = gen.image_url || '';
  const folder = gen.folder || gen.expected_folder || '';
  const filename = gen.filename || (gen.expected_path ? gen.expected_path.split('/').pop() : '');
  const fullPath = gen.image_path || gen.expected_path || '';

  return (
    <div style={{
      position: 'fixed', top: 130, left: '50%', transform: 'translateX(-50%)',
      width: 720, maxWidth: 'calc(100vw - 40px)', maxHeight: '82vh',
      background: '#141a28', border: `1px solid ${accent}77`, borderRadius: 14,
      boxShadow: `0 18px 60px rgba(0,0,0,0.6), 0 0 0 1px ${accent}44`,
      zIndex: 48, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '14px 16px', borderBottom: '1px solid #232d45',
                    display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className={isWorking ? 'animate-pulse-soft' : ''} style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: `${accent}22`, color: accent,
        }}>🎨</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5',
                        display: 'flex', alignItems: 'center', gap: 8 }}>
            {isWorking ? 'Generating image…' : isDone ? 'Image saved' : 'Generation failed'}
            {gen.total > 0 && (
              <span style={{ fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                             padding: '2px 8px', borderRadius: 999,
                             background: `${accent}22`, color: accent,
                             border: `1px solid ${accent}55`, fontWeight: 600 }}>
                POST {gen.index + 1} / {gen.total}
              </span>
            )}
            {gen.improved && (
              <span style={{ fontSize: 9.5, color: '#a78bfa', fontWeight: 700,
                             padding: '2px 7px', borderRadius: 999,
                             background: 'rgba(167,139,250,0.15)',
                             border: '1px solid rgba(167,139,250,0.35)',
                             textTransform: 'uppercase', letterSpacing: '0.15em' }}>
                Improved prompt
              </span>
            )}
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase',
                        letterSpacing: '0.2em', marginTop: 2 }}>
            {isWorking ? 'Gemini via Chrome · expect ~45 s' :
             isDone    ? 'Saved to disk · served over /projects/' :
                         'See details below'}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4,
        }}>✕</button>
      </div>

      {/* Folder / path banner */}
      <div style={{
        padding: '10px 14px', borderBottom: '1px dashed #232d45',
        background: 'rgba(255,107,53,0.05)', display: 'flex', alignItems: 'center', gap: 10,
      }}>
        <span style={{ fontSize: 16, color: '#ff6b35' }}>📁</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 9.5, color: '#7f8aa3', textTransform: 'uppercase',
                        letterSpacing: '0.2em', fontWeight: 700, marginBottom: 2 }}>
            Destination
          </div>
          <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12,
                        color: '#e6ebf5', lineHeight: 1.5, wordBreak: 'break-all' }}>
            {gen.parent_folder || '(today)'}/<span style={{ color: '#ff6b35' }}>{folder || '?'}</span>/<span style={{ color: accent }}>{filename || '…'}</span>
          </div>
          {fullPath && (
            <div style={{ fontSize: 10, color: '#7f8aa3', marginTop: 3, wordBreak: 'break-all' }}>
              {fullPath}
            </div>
          )}
        </div>
      </div>

      {/* Image preview */}
      <div style={{
        flex: 1, minHeight: 0, overflow: 'auto',
        background: '#0a0f1c', padding: 16,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        {isWorking && (
          <div style={{ color: '#7f8aa3', textAlign: 'center', padding: 20 }}>
            <span className="animate-spin" style={{ display: 'inline-block', fontSize: 36, color: accent }}>⚙</span>
            <div style={{ marginTop: 14, fontSize: 12 }}>
              Gemini is composing the frame from your prompt…
            </div>
            <div style={{ marginTop: 6, fontSize: 10.5, color: '#4e5872',
                          fontFamily: 'JetBrains Mono, monospace' }}>
              {gen.prompt.slice(0, 160)}{gen.prompt.length > 160 && '…'}
            </div>
          </div>
        )}
        {isDone && url && (
          <img
            src={`${url}?cb=${Date.now()}`}
            alt={filename}
            style={{
              maxWidth: '100%', maxHeight: '100%',
              borderRadius: 8, border: '1px solid #232d45',
              boxShadow: '0 14px 40px rgba(0,0,0,0.55)',
            }}
          />
        )}
        {isError && (
          <div style={{ color: '#ef4444', textAlign: 'center', fontSize: 12, maxWidth: 480 }}>
            <div style={{ fontSize: 32 }}>⚠</div>
            <div style={{ marginTop: 8, fontWeight: 700 }}>Image generation failed.</div>
            <div style={{ marginTop: 6, fontSize: 11, color: '#d0d6e8',
                          fontFamily: 'JetBrains Mono, monospace' }}>
              {gen.result || '(no details)'}
            </div>
          </div>
        )}
      </div>

      {/* Actions — approve-or-regenerate is the primary choice. */}
      <div style={{ padding: 12, borderTop: '1px solid #232d45',
                    display: 'grid',
                    gridTemplateColumns: viewOnly ? 'repeat(3, 1fr)' : 'repeat(4, 1fr)', gap: 6 }}>
        <button disabled={isWorking || !isDone} onClick={onApprove}
          title="This image looks good — keep it."
          style={{
            fontSize: 11.5, fontWeight: 800, padding: '10px 10px', borderRadius: 8,
            background: (isWorking || !isDone) ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.22)',
            color: (isWorking || !isDone) ? '#4e5872' : '#10b981',
            border: `1px solid ${(isWorking || !isDone) ? '#232d45' : 'rgba(16,185,129,0.5)'}`,
            cursor: (isWorking || !isDone) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.12em',
            boxShadow: (isWorking || !isDone) ? 'none' : '0 0 0 1px rgba(16,185,129,0.18)',
          }}>✓ Approve</button>
        <button disabled={isWorking} onClick={onRegenerate}
          title="Not good enough — regenerate the image."
          style={{
            fontSize: 11, fontWeight: 700, padding: '10px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(167,139,250,0.06)' : 'rgba(167,139,250,0.18)',
            color: isWorking ? '#4e5872' : '#a78bfa',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(167,139,250,0.35)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>↻ Regenerate</button>
        <button disabled={isWorking} onClick={onOpenFolder}
          style={{
            fontSize: 11, fontWeight: 700, padding: '10px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(0,229,255,0.06)' : 'rgba(0,229,255,0.14)',
            color: isWorking ? '#4e5872' : '#00e5ff',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(0,229,255,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>📂 Folder</button>
        {!viewOnly && (
          <button disabled={isWorking} onClick={onNextPost}
            style={{
              fontSize: 11, fontWeight: 700, padding: '10px 10px', borderRadius: 8,
              background: isWorking ? 'rgba(251,191,36,0.06)' : 'rgba(251,191,36,0.2)',
              color: isWorking ? '#4e5872' : '#fbbf24',
              border: `1px solid ${isWorking ? '#232d45' : 'rgba(251,191,36,0.4)'}`,
              cursor: isWorking ? 'not-allowed' : 'pointer',
              textTransform: 'uppercase', letterSpacing: '0.1em',
            }}>▸ Next post</button>
        )}
      </div>
    </div>
  );
}

function PostImprovementOverlay({ imp, onClose, onAccept, onAcceptAndGenerate, onReject, onRedo, onSpeak }: {
  imp: PostImprovement;
  onClose: () => void;
  onAccept: () => void;
  onAcceptAndGenerate: () => void;
  onReject: () => void;
  onRedo: (focus: string) => void;
  onSpeak: (text: string) => void;
}) {
  const isWorking = imp.status === 'working';
  const finalised = imp.status === 'accepted' || imp.status === 'rejected';
  const accent = imp.status === 'accepted' ? '#10b981'
               : imp.status === 'rejected' ? '#ef4444'
               : '#fbbf24';
  return (
    <div style={{
      position: 'fixed', top: 130, left: '50%', transform: 'translateX(-50%)',
      width: 780, maxWidth: 'calc(100vw - 40px)', maxHeight: '80vh',
      background: '#141a28', border: `1px solid ${accent}77`, borderRadius: 14,
      boxShadow: `0 18px 60px rgba(0,0,0,0.6), 0 0 0 1px ${accent}44`,
      zIndex: 50, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '14px 16px', borderBottom: '1px solid #232d45',
                    display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className={isWorking ? 'animate-pulse-soft' : ''} style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: `${accent}22`, color: accent,
        }}>✎</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5',
                        display:'flex', alignItems:'center', gap: 8 }}>
            Gemma improved NB2 prompt {isWorking ? '…' : ''}
            {imp.total > 0 && (
              <span style={{ fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                             padding: '2px 8px', borderRadius: 999,
                             background: `${accent}22`, color: accent,
                             border: `1px solid ${accent}55`, fontWeight: 600 }}>
                POST {imp.index + 1} / {imp.total}
              </span>
            )}
            {imp.status === 'accepted' && (
              <span style={{ fontSize: 10, color: '#10b981', fontWeight: 700,
                             textTransform: 'uppercase', letterSpacing: '0.18em' }}>✓ Accepted</span>
            )}
            {imp.status === 'rejected' && (
              <span style={{ fontSize: 10, color: '#ef4444', fontWeight: 700,
                             textTransform: 'uppercase', letterSpacing: '0.18em' }}>✗ Rejected</span>
            )}
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.2em' }}>
            local brain rewrote for NB2 {imp.focus ? `· focus: ${imp.focus}` : ''}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4,
        }}>✕</button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
        {isWorking && !imp.improved && (
          <div style={{ color: '#7f8aa3', fontSize: 12, textAlign: 'center', padding: 30 }}>
            <span className="animate-spin" style={{ display: 'inline-block', fontSize: 20 }}>⚙</span>
            <div style={{ marginTop: 10 }}>Gemma is rewriting the prompt…</div>
          </div>
        )}
        {imp.improved && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div style={{ background: '#0a0f1c', border: '1px solid #232d45', borderRadius: 10, padding: 12 }}>
              <div style={{ fontSize: 9.5, color: '#ef4444', textTransform: 'uppercase',
                            letterSpacing: '0.22em', fontWeight: 700, marginBottom: 8,
                            display:'flex', alignItems:'center', gap: 6 }}>
                <span style={{ width: 3, height: 12, background: '#ef4444', borderRadius: 2 }}/>
                Original
              </div>
              <div style={{ fontSize: 12, color: '#d0d6e8', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                {imp.original}
              </div>
            </div>
            <div style={{ background: '#0a0f1c', border: `1px solid ${accent}55`,
                          borderRadius: 10, padding: 12 }}>
              <div style={{ fontSize: 9.5, color: accent, textTransform: 'uppercase',
                            letterSpacing: '0.22em', fontWeight: 700, marginBottom: 8,
                            display:'flex', alignItems:'center', gap: 6 }}>
                <span style={{ width: 3, height: 12, background: accent, borderRadius: 2 }}/>
                Improved (Gemma)
              </div>
              <div style={{ fontSize: 12, color: '#e6ebf5', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                {imp.improved}
              </div>
            </div>
          </div>
        )}
        {imp.changes.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 10, color: '#a78bfa', textTransform: 'uppercase',
                          letterSpacing: '0.22em', fontWeight: 700, marginBottom: 8,
                          display:'flex', alignItems:'center', gap: 6 }}>
              <span style={{ width: 3, height: 12, background: '#a78bfa', borderRadius: 2 }}/>
              Key changes
            </div>
            <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
              {imp.changes.map((c, i) => (
                <li key={i} style={{ fontSize: 12, color: '#d0d6e8', padding: '4px 0 4px 16px',
                                     position:'relative', lineHeight: 1.5 }}>
                  <span style={{ position: 'absolute', left: 0, color: '#a78bfa' }}>→</span>
                  {c}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div style={{ padding: 12, borderTop: '1px solid #232d45',
                    display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 6 }}>
        <button disabled={isWorking || !imp.improved} onClick={() => onSpeak(imp.improved)}
          style={{
            fontSize: 10.5, fontWeight: 700, padding: '9px 8px', borderRadius: 8,
            background: (!imp.improved || isWorking) ? 'rgba(0,229,255,0.06)' : 'rgba(0,229,255,0.16)',
            color: (!imp.improved || isWorking) ? '#4e5872' : '#00e5ff',
            border: `1px solid ${(!imp.improved || isWorking) ? '#232d45' : 'rgba(0,229,255,0.3)'}`,
            cursor: (!imp.improved || isWorking) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.08em',
          }}>▶ Read</button>
        <button disabled={isWorking || finalised} onClick={onAccept}
          title="Swap the improved prompt in — don't generate yet."
          style={{
            fontSize: 10.5, fontWeight: 700, padding: '9px 8px', borderRadius: 8,
            background: (isWorking || finalised) ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.18)',
            color: (isWorking || finalised) ? '#4e5872' : '#10b981',
            border: `1px solid ${(isWorking || finalised) ? '#232d45' : 'rgba(16,185,129,0.35)'}`,
            cursor: (isWorking || finalised) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.08em',
          }}>✓ Accept</button>
        <button disabled={isWorking || finalised} onClick={onAcceptAndGenerate}
          title="Swap in the improved prompt AND fire image generation — one-click approval."
          style={{
            fontSize: 10.5, fontWeight: 800, padding: '9px 8px', borderRadius: 8,
            background: (isWorking || finalised) ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.26)',
            color: (isWorking || finalised) ? '#4e5872' : '#10b981',
            border: `1px solid ${(isWorking || finalised) ? '#232d45' : 'rgba(16,185,129,0.55)'}`,
            cursor: (isWorking || finalised) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.08em',
            boxShadow: (isWorking || finalised) ? 'none' : '0 0 0 1px rgba(16,185,129,0.15)',
          }}>✓ Accept & 🎨 Generate</button>
        <button disabled={isWorking || finalised} onClick={onReject}
          style={{
            fontSize: 10.5, fontWeight: 700, padding: '9px 8px', borderRadius: 8,
            background: (isWorking || finalised) ? 'rgba(239,68,68,0.06)' : 'rgba(239,68,68,0.16)',
            color: (isWorking || finalised) ? '#4e5872' : '#ef4444',
            border: `1px solid ${(isWorking || finalised) ? '#232d45' : 'rgba(239,68,68,0.3)'}`,
            cursor: (isWorking || finalised) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.08em',
          }}>✗ Reject</button>
        <button disabled={isWorking} onClick={() => {
          const f = window.prompt('Optional focus for the re-redo (e.g. "more cinematic", "tighter framing"):', imp.focus || '');
          onRedo((f || '').trim());
        }}
          style={{
            fontSize: 10.5, fontWeight: 700, padding: '9px 8px', borderRadius: 8,
            background: isWorking ? 'rgba(167,139,250,0.06)' : 'rgba(167,139,250,0.16)',
            color: isWorking ? '#4e5872' : '#a78bfa',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(167,139,250,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.08em',
          }}>↻ Redo</button>
      </div>
    </div>
  );
}

function PostReviewOverlay({ review, onClose, onSpeak, onNext, onPrev, onGenerate, onImprove }: {
  review: PostReview;
  onClose: () => void;
  onSpeak: (text: string) => void;
  onNext: () => void;
  onPrev: () => void;
  onGenerate: () => void;
  onImprove: () => void;
}) {
  const isWorking = review.status === 'working';
  const sections: { label: string; body: string; color: string }[] = (() => {
    const text = review.analysis || '';
    const labels = [
      { k: 'SUMMARY',      c: '#00e5ff' },
      { k: 'KEY ELEMENTS', c: '#fbbf24' },
      { k: 'STRENGTHS',    c: '#10b981' },
      { k: 'MY TAKE',      c: '#ff6b35' },
    ];
    const regex = new RegExp(`(^|\\n)\\s*(${labels.map(l => l.k).join('|')})\\s*:?\\s*\\n`, 'gi');
    const out: { label: string; body: string; color: string }[] = [];
    const matches = [...text.matchAll(regex)];
    matches.forEach((m, i) => {
      const start = (m.index || 0) + m[0].length;
      const end = i + 1 < matches.length ? matches[i+1].index! : text.length;
      const label = m[2].toUpperCase();
      const color = labels.find(l => l.k === label)?.c || '#e6ebf5';
      out.push({ label, body: text.slice(start, end).trim(), color });
    });
    return out;
  })();

  return (
    <div style={{
      position: 'fixed', top: 130, left: '50%', transform: 'translateX(-50%)',
      width: 620, maxWidth: 'calc(100vw - 40px)', maxHeight: '78vh',
      background: '#141a28', border: '1px solid rgba(255,107,53,0.55)', borderRadius: 14,
      boxShadow: '0 18px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,107,53,0.25)',
      zIndex: 45, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '14px 16px', borderBottom: '1px solid #232d45',
                    display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className={isWorking ? 'animate-pulse-soft' : ''} style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: 'rgba(255,107,53,0.18)', color: '#ff6b35',
        }}>📝</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5',
                        display: 'flex', alignItems: 'center', gap: 8 }}>
            Post Review {isWorking ? '…' : ''}
            {review.total > 0 && (
              <span style={{
                fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                padding: '2px 8px', borderRadius: 999,
                background: 'rgba(255,107,53,0.18)', color: '#ff6b35',
                border: '1px solid rgba(255,107,53,0.35)',
                fontWeight: 600, letterSpacing: '0.1em',
              }}>{review.index + 1} / {review.total}</span>
            )}
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.2em' }}>
            local brain · source: {review.source || 'auto'}{review.section ? ` · ${review.section}` : ''}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4,
        }}>✕</button>
      </div>

      <div style={{ padding: 12, borderBottom: '1px dashed #232d45',
                    background: 'rgba(255,107,53,0.05)' }}>
        <div style={{ fontSize: 9.5, color: '#7f8aa3', textTransform: 'uppercase',
                      letterSpacing: '0.2em', marginBottom: 5, fontWeight: 700 }}>Post prompt</div>
        <div style={{ fontSize: 12.5, color: '#e6ebf5', lineHeight: 1.55, whiteSpace: 'pre-wrap' }}>
          {review.prompt_text}
        </div>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
        {isWorking && !review.analysis && (
          <div style={{ color: '#7f8aa3', fontSize: 12, textAlign: 'center', padding: 24 }}>
            <span className="animate-spin" style={{ display: 'inline-block', fontSize: 20 }}>⚙</span>
            <div style={{ marginTop: 10 }}>Gemma is reviewing this post…</div>
          </div>
        )}
        {sections.map((s, i) => (
          <div key={i} style={{ marginBottom: 14 }}>
            <div style={{
              fontSize: 10, color: s.color, textTransform: 'uppercase',
              letterSpacing: '0.22em', fontWeight: 700, marginBottom: 6,
              display: 'flex', alignItems: 'center', gap: 6,
            }}>
              <span style={{ width: 3, height: 14, background: s.color, borderRadius: 2 }}/>
              {s.label}
            </div>
            <div style={{
              fontSize: 12.5, color: '#e6ebf5', lineHeight: 1.7,
              whiteSpace: 'pre-wrap', paddingLeft: 9,
            }}>{s.body}</div>
          </div>
        ))}
      </div>

      <div style={{ padding: 12, borderTop: '1px solid #232d45',
                    display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
        <button disabled={!review.my_take || isWorking} onClick={() => onSpeak(review.my_take || '')}
          style={{
            fontSize: 11, fontWeight: 700, padding: '9px 10px', borderRadius: 8,
            background: (!review.my_take || isWorking) ? 'rgba(0,229,255,0.06)' : 'rgba(0,229,255,0.16)',
            color: (!review.my_take || isWorking) ? '#4e5872' : '#00e5ff',
            border: `1px solid ${(!review.my_take || isWorking) ? '#232d45' : 'rgba(0,229,255,0.3)'}`,
            cursor: (!review.my_take || isWorking) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>▶ Read MY TAKE</button>
        <button disabled={isWorking} onClick={onImprove}
          title="Gemma rewrites this prompt as a stronger NB2 / Gemini prompt — you review before accepting."
          style={{
            fontSize: 11, fontWeight: 700, padding: '9px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(251,191,36,0.06)' : 'rgba(251,191,36,0.2)',
            color: isWorking ? '#4e5872' : '#fbbf24',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(251,191,36,0.45)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>✎ Improve prompt</button>
        <button disabled={isWorking} onClick={onGenerate}
          title="Approve and generate the image. Uses the current prompt (original or improved)."
          style={{
            fontSize: 11.5, fontWeight: 800, padding: '9px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.2)',
            color: isWorking ? '#4e5872' : '#10b981',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(16,185,129,0.45)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>🎨 Generate image</button>
      </div>
      <div style={{ padding: '8px 12px 12px', display: 'flex', gap: 6,
                    borderTop: '1px dashed rgba(35,45,69,0.6)' }}>
        <button disabled={isWorking || review.index <= 0} onClick={onPrev}
          style={{
            flex: 1, fontSize: 10.5, fontWeight: 700, padding: '7px 10px', borderRadius: 8,
            background: 'rgba(127,138,163,0.1)', color: '#7f8aa3',
            border: '1px solid rgba(127,138,163,0.25)', cursor: 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>◂ Previous post</button>
        <button disabled={isWorking || review.index + 1 >= review.total} onClick={onNext}
          style={{
            flex: 2, fontSize: 11.5, fontWeight: 700, padding: '7px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(251,191,36,0.06)' : 'rgba(251,191,36,0.2)',
            color: isWorking ? '#4e5872' : '#fbbf24',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(251,191,36,0.4)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.12em',
          }}>▸ Next post + review</button>
      </div>
    </div>
  );
}

function VoiceStatus({ state, lastReply }: {
  state: 'idle' | 'listening' | 'thinking' | 'speaking';
  lastReply: string;
}) {
  // Five bars, each with a different animation delay, so the speaking /
  // listening states get a lively equalizer look. Colors track the state.
  const color = state === 'listening' ? '#00e5ff'
              : state === 'thinking'  ? '#a78bfa'
              : state === 'speaking'  ? '#ff6b35'
              :                         '#2a3854';
  const label = state === 'listening' ? 'LISTENING…'
              : state === 'thinking'  ? 'GEMMA THINKING…'
              : state === 'speaking'  ? 'LOTUS SPEAKING'
              :                         'IDLE';
  const animating = state !== 'idle';
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 10,
      padding: '4px 12px', borderRadius: 999,
      background: `${color}14`, border: `1px solid ${color}55`,
      fontSize: 10, letterSpacing: '0.2em', fontWeight: 700,
      color, textTransform: 'uppercase',
      transition: 'all 0.25s',
    }}>
      <div style={{ display: 'inline-flex', alignItems: 'flex-end', gap: 2, height: 14 }}>
        {[0,1,2,3,4].map(i => (
          <span key={i} style={{
            display: 'inline-block', width: 2.5, background: color, borderRadius: 2,
            height: animating ? undefined : 3,
            animation: animating ? `lotus-eq 1.1s ${i*0.13}s ease-in-out infinite` : 'none',
          }}/>
        ))}
      </div>
      <span>{label}</span>
      {state === 'speaking' && lastReply && (
        <span style={{
          fontSize: 10, color: '#e6ebf5', textTransform: 'none', fontWeight: 400,
          letterSpacing: 0, maxWidth: 420, whiteSpace: 'nowrap',
          overflow: 'hidden', textOverflow: 'ellipsis',
        }}>“{lastReply}”</span>
      )}
    </div>
  );
}

function NewsOverlay({ news, onClose, onSpeak, onAnalyze }: {
  news: NewsPayload;
  onClose: () => void;
  onSpeak: (text: string) => void;
  onAnalyze: (item: NewsItem, question?: string) => void;
}) {
  return (
    <div style={{
      position: 'fixed', top: 130, right: 24, width: 440, maxHeight: '70vh',
      background: '#141a28', border: '1px solid rgba(251,191,36,0.45)', borderRadius: 14,
      boxShadow: '0 18px 60px rgba(0,0,0,0.55), 0 0 0 1px rgba(251,191,36,0.2)',
      zIndex: 40, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        padding: '14px 16px', borderBottom: '1px solid #232d45',
        display: 'flex', alignItems: 'center', gap: 10,
      }}>
        <span style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: 'rgba(251,191,36,0.15)', color: '#fbbf24',
        }}>📰</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
            {news.topic ? news.topic.charAt(0).toUpperCase() + news.topic.slice(1) : 'Trending'} News
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.18em' }}>
            {news.items.length} headlines · via Google News
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4,
        }}>✕</button>
      </div>
      <div style={{ overflow: 'auto', padding: 14, flex: 1 }}>
        {news.error && <div style={{ color: '#ef4444', fontSize: 12 }}>⚠ {news.error}</div>}
        {news.items.map((h, i) => (
          <div key={i} style={{
            padding: '10px 0', borderBottom: i < news.items.length - 1 ? '1px solid #1a2238' : 'none',
            display: 'flex', gap: 10,
          }}>
            <span style={{
              fontSize: 10, color: '#fbbf24', fontWeight: 700, fontFamily: 'JetBrains Mono, monospace',
              width: 18, flexShrink: 0, marginTop: 2,
            }}>{String(i+1).padStart(2,'0')}</span>
            <div style={{ flex: 1 }}>
              <a href={h.url} target="_blank" rel="noreferrer"
                 style={{ fontSize: 13, fontWeight: 600, color: '#e6ebf5', textDecoration: 'none',
                          display: 'block', lineHeight: 1.4 }}>
                {h.title}
              </a>
              {(h.source || h.published) && (
                <div style={{ fontSize: 10, color: '#7f8aa3', marginTop: 3 }}>
                  {h.source}{h.source && h.published && ' · '}{h.published && new Date(h.published).toLocaleString('en-US', { hour12: false })}
                </div>
              )}
              <div style={{ display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
                <button onClick={() => onSpeak(h.title)} style={{
                  fontSize: 9, padding: '2px 8px', borderRadius: 999,
                  background: 'rgba(0,229,255,0.12)', color: '#00e5ff',
                  border: '1px solid rgba(0,229,255,0.25)', cursor: 'pointer',
                  textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 600,
                }}>▶ Read aloud</button>
                <button onClick={() => onAnalyze(h)} style={{
                  fontSize: 9, padding: '2px 8px', borderRadius: 999,
                  background: 'rgba(167,139,250,0.15)', color: '#a78bfa',
                  border: '1px solid rgba(167,139,250,0.3)', cursor: 'pointer',
                  textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 600,
                }}>🧠 Analyze with Gemma</button>
                <button onClick={() => {
                  const q = window.prompt(`Ask Gemma about:\n"${h.title.slice(0, 80)}"\n\nWhat do you want to know?`, '');
                  if (q && q.trim()) onAnalyze(h, q.trim());
                }} style={{
                  fontSize: 9, padding: '2px 8px', borderRadius: 999,
                  background: 'rgba(255,107,53,0.12)', color: '#ff6b35',
                  border: '1px solid rgba(255,107,53,0.25)', cursor: 'pointer',
                  textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 600,
                }}>❓ Ask question</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function NewsAnalysisOverlay({ analysis, onClose, onSpeak, onFollowUp, onNext, onPrev }: {
  analysis: NewsAnalysis;
  onClose: () => void;
  onSpeak: (text: string) => void;
  onFollowUp: (question: string) => void;
  onNext: () => void;
  onPrev: () => void;
}) {
  const isWorking = analysis.status === 'working';
  // Split sections by the ALL-CAPS labels Gemma produces.
  const sections: { label: string; body: string }[] = (() => {
    const text = analysis.analysis || '';
    if (!text) return [];
    const labels = ['SUMMARY', 'KEY FACTS', 'WHY IT MATTERS', 'MY TAKE'];
    const regex = new RegExp(`(^|\\n)\\s*(${labels.join('|')})\\s*:?\\s*\\n`, 'gi');
    const parts: { label: string; body: string }[] = [];
    const matches = [...text.matchAll(regex)];
    if (!matches.length) return [{ label: 'Analysis', body: text.trim() }];
    matches.forEach((m, i) => {
      const start = (m.index || 0) + m[0].length;
      const end = i + 1 < matches.length ? matches[i+1].index! : text.length;
      parts.push({ label: m[2].toUpperCase(), body: text.slice(start, end).trim() });
    });
    return parts;
  })();
  const myTake = sections.find(s => s.label === 'MY TAKE')?.body || '';

  return (
    <div style={{
      position: 'fixed', top: 130, left: '50%', transform: 'translateX(-50%)',
      width: 580, maxWidth: 'calc(100vw - 40px)', maxHeight: '78vh',
      background: '#141a28', border: '1px solid rgba(167,139,250,0.5)', borderRadius: 14,
      boxShadow: '0 18px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(167,139,250,0.25)',
      zIndex: 45, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '14px 16px', borderBottom: '1px solid #232d45',
                    display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className={isWorking ? 'animate-pulse-soft' : ''} style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: 'rgba(167,139,250,0.18)', color: '#a78bfa',
        }}>🧠</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
            Gemma's Analysis {isWorking ? '…' : ''}
            {typeof analysis.cursor === 'number' && typeof analysis.total === 'number' && analysis.total > 0 && (
              <span style={{
                marginLeft: 10, fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                padding: '2px 8px', borderRadius: 999,
                background: 'rgba(167,139,250,0.15)', color: '#a78bfa',
                border: '1px solid rgba(167,139,250,0.3)', fontWeight: 600,
                textTransform: 'uppercase', letterSpacing: '0.1em',
              }}>
                {analysis.cursor + 1} / {analysis.total}
              </span>
            )}
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.18em' }}>
            local brain · {analysis.has_article ? 'full article parsed' : 'headline-only mode'}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4,
        }}>✕</button>
      </div>

      <div style={{ padding: 12, borderBottom: '1px dashed #232d45',
                    background: 'rgba(167,139,250,0.05)' }}>
        <div style={{ fontSize: 9.5, color: '#7f8aa3', textTransform: 'uppercase',
                      letterSpacing: '0.2em', marginBottom: 5, fontWeight: 700 }}>Headline</div>
        <div style={{ fontSize: 13, color: '#e6ebf5', fontWeight: 600, lineHeight: 1.4 }}>
          {analysis.title}
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 5, flexWrap: 'wrap' }}>
          {analysis.source && (
            <span style={{ fontSize: 10, color: '#fbbf24', fontWeight: 600 }}>
              {analysis.source}
            </span>
          )}
          {analysis.published && (
            <span style={{ fontSize: 10, color: '#7f8aa3' }}>
              {new Date(analysis.published).toLocaleString('en-US', { hour12: false })}
            </span>
          )}
          {analysis.url && (
            <a href={analysis.url} target="_blank" rel="noreferrer"
               style={{ fontSize: 10, color: '#00e5ff', textDecoration: 'none' }}>
              Open source →
            </a>
          )}
        </div>
        {analysis.entities && analysis.entities.length > 0 && (
          <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            <span style={{ fontSize: 9, color: '#7f8aa3', textTransform: 'uppercase',
                           letterSpacing: '0.18em', fontWeight: 700, marginRight: 4,
                           alignSelf: 'center' }}>anchors</span>
            {analysis.entities.map((e, i) => (
              <span key={i} style={{
                fontSize: 10, padding: '2px 8px', borderRadius: 999,
                background: 'rgba(0,229,255,0.1)', color: '#00e5ff',
                border: '1px solid rgba(0,229,255,0.25)',
                fontFamily: 'JetBrains Mono, monospace',
              }}>{e}</span>
            ))}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
        {isWorking && !analysis.analysis && (
          <div style={{ color: '#7f8aa3', fontSize: 12, textAlign: 'center', padding: 24 }}>
            <span className="animate-spin" style={{ display: 'inline-block', fontSize: 20 }}>⚙</span>
            <div style={{ marginTop: 10 }}>Gemma is fetching and processing the article…</div>
          </div>
        )}
        {sections.map((s, i) => {
          const color = s.label === 'SUMMARY'   ? '#00e5ff'
                      : s.label === 'KEY FACTS' ? '#fbbf24'
                      : s.label === 'WHY IT MATTERS' ? '#a78bfa'
                      : s.label === 'MY TAKE'   ? '#ff6b35'
                      : '#e6ebf5';
          return (
            <div key={i} style={{ marginBottom: 14 }}>
              <div style={{
                fontSize: 10, color, textTransform: 'uppercase',
                letterSpacing: '0.22em', fontWeight: 700, marginBottom: 6,
                display: 'flex', alignItems: 'center', gap: 6,
              }}>
                <span style={{ width: 3, height: 14, background: color, borderRadius: 2 }}/>
                {s.label}
              </div>
              <div style={{
                fontSize: 12.5, color: '#e6ebf5', lineHeight: 1.7,
                whiteSpace: 'pre-wrap', paddingLeft: 9,
              }}>{s.body}</div>
            </div>
          );
        })}
      </div>

      <div style={{ padding: 12, borderTop: '1px solid #232d45', display: 'flex', gap: 6 }}>
        <button disabled={!myTake || isWorking} onClick={() => onSpeak(myTake || analysis.analysis)}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: (!myTake || isWorking) ? 'rgba(0,229,255,0.06)' : 'rgba(0,229,255,0.16)',
            color:       (!myTake || isWorking) ? '#4e5872' : '#00e5ff',
            border: `1px solid ${(!myTake || isWorking) ? '#232d45' : 'rgba(0,229,255,0.3)'}`,
            cursor: (!myTake || isWorking) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>▶ Read MY TAKE</button>
        <button disabled={isWorking} onClick={() => {
          const q = window.prompt('Follow-up question for Gemma:', '');
          if (q && q.trim()) onFollowUp(q.trim());
        }}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(167,139,250,0.06)' : 'rgba(167,139,250,0.16)',
            color:       isWorking ? '#4e5872' : '#a78bfa',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(167,139,250,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>❓ Follow-up</button>
        <button disabled={isWorking} onClick={() => onFollowUp(`give me a different angle on "${analysis.title}"`)}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(255,107,53,0.06)' : 'rgba(255,107,53,0.16)',
            color:       isWorking ? '#4e5872' : '#ff6b35',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(255,107,53,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>↻ Different angle</button>
      </div>

      <div style={{ padding: '8px 12px 12px', display: 'flex', gap: 6,
                    borderTop: '1px dashed rgba(35,45,69,0.6)' }}>
        <button
          disabled={isWorking || (typeof analysis.cursor === 'number' && analysis.cursor <= 0)}
          onClick={onPrev}
          style={{
            flex: 1, fontSize: 10.5, fontWeight: 700, padding: '7px 10px', borderRadius: 8,
            background: 'rgba(127,138,163,0.1)', color: '#7f8aa3',
            border: '1px solid rgba(127,138,163,0.25)', cursor: 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>◂ Previous news</button>
        <button
          disabled={isWorking ||
            (typeof analysis.cursor === 'number' && typeof analysis.total === 'number'
              && analysis.cursor + 1 >= analysis.total)}
          onClick={onNext}
          style={{
            flex: 2, fontSize: 11.5, fontWeight: 700, padding: '7px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(251,191,36,0.06)' : 'rgba(251,191,36,0.2)',
            color: isWorking ? '#4e5872' : '#fbbf24',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(251,191,36,0.4)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.12em',
          }}>▸ Next news + analyse</button>
      </div>
    </div>
  );
}

function ClaudeOverlay({ claude, onClose, onRetry, onApprove, onSpeak }: {
  claude: ClaudeSuggestion;
  onClose: () => void;
  onRetry: () => void;
  onApprove: () => void;
  onSpeak: (text: string) => void;
}) {
  const scoreColor = claude.score >= 8 ? '#10b981' : claude.score >= 5 ? '#fbbf24' : '#ef4444';
  const isWorking = claude.status === 'working' || claude.status === 'streaming';
  const isError   = claude.status === 'error';
  return (
    <div style={{
      position: 'fixed', top: 130, left: 24, width: 500, maxHeight: '72vh',
      background: '#141a28', border: '1px solid rgba(232,121,249,0.5)', borderRadius: 14,
      boxShadow: '0 18px 60px rgba(0,0,0,0.55), 0 0 0 1px rgba(232,121,249,0.25)',
      zIndex: 40, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        padding: '14px 16px', borderBottom: '1px solid #232d45',
        display: 'flex', alignItems: 'center', gap: 10,
      }}>
        <span style={{
          width: 34, height: 34, borderRadius: 8, display: 'flex',
          alignItems: 'center', justifyContent: 'center', fontSize: 18,
          background: 'rgba(232,121,249,0.15)', color: '#e879f9',
        }} className={isWorking ? 'animate-pulse-soft' : ''}>🧠</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e6ebf5' }}>
            Claude is suggesting{isWorking ? '…' : ':'}
          </div>
          <div style={{ fontSize: 10, color: '#7f8aa3', textTransform: 'uppercase', letterSpacing: '0.18em' }}>
            {claude.status === 'working' ? 'sending query to Claude.ai' :
             claude.status === 'streaming' ? 'response streaming in' :
             claude.status === 'error'    ? 'capture error' : 'response captured'}
          </div>
        </div>
        {!isWorking && !isError && (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            padding: '6px 10px', borderRadius: 8,
            background: `${scoreColor}22`, border: `1px solid ${scoreColor}55`,
          }}>
            <span style={{ fontSize: 10, color: scoreColor, fontWeight: 700,
                           textTransform: 'uppercase', letterSpacing: '0.18em' }}>Score</span>
            <span style={{ fontSize: 20, color: scoreColor, fontWeight: 800,
                           fontFamily: 'JetBrains Mono, monospace', lineHeight: 1 }}>
              {claude.score.toFixed(1)}
            </span>
            <span style={{ fontSize: 9, color: scoreColor, opacity: 0.7 }}>of 10</span>
          </div>
        )}
        <button onClick={onClose} style={{
          background: 'transparent', border: 'none', color: '#7f8aa3',
          fontSize: 18, cursor: 'pointer', padding: 4, marginLeft: 6,
        }}>✕</button>
      </div>

      <div style={{ padding: 14, borderBottom: '1px dashed #232d45',
                    background: 'rgba(232,121,249,0.04)' }}>
        <div style={{ fontSize: 9.5, color: '#7f8aa3', textTransform: 'uppercase',
                      letterSpacing: '0.2em', marginBottom: 5, fontWeight: 700 }}>Your question</div>
        <div style={{ fontSize: 12, color: '#e6ebf5', lineHeight: 1.5 }}>{claude.query}</div>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 14 }}>
        {isWorking && !claude.response && (
          <div style={{ color: '#7f8aa3', fontSize: 12, textAlign: 'center', padding: 24 }}>
            <span className="animate-spin" style={{ display: 'inline-block', fontSize: 20 }}>⚙</span>
            <div style={{ marginTop: 10 }}>Claude is composing a reply…</div>
          </div>
        )}
        {claude.response && (
          <div style={{ fontSize: 13, color: '#e6ebf5', lineHeight: 1.65, whiteSpace: 'pre-wrap' }}>
            {claude.response}
          </div>
        )}
      </div>

      <div style={{ padding: 12, borderTop: '1px solid #232d45', display: 'flex', gap: 6 }}>
        <button disabled={!claude.response || isWorking} onClick={() => onSpeak(claude.response)}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: (!claude.response || isWorking) ? 'rgba(0,229,255,0.06)' : 'rgba(0,229,255,0.16)',
            color: (!claude.response || isWorking) ? '#4e5872' : '#00e5ff',
            border: `1px solid ${(!claude.response || isWorking) ? '#232d45' : 'rgba(0,229,255,0.3)'}`,
            cursor: (!claude.response || isWorking) ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>▶ Read Aloud</button>
        <button disabled={isWorking} onClick={onApprove}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(16,185,129,0.06)' : 'rgba(16,185,129,0.16)',
            color: isWorking ? '#4e5872' : '#10b981',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(16,185,129,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>✓ Use This</button>
        <button disabled={isWorking} onClick={onRetry}
          style={{
            flex: 1, fontSize: 11, fontWeight: 700, padding: '8px 10px', borderRadius: 8,
            background: isWorking ? 'rgba(167,139,250,0.06)' : 'rgba(167,139,250,0.16)',
            color: isWorking ? '#4e5872' : '#a78bfa',
            border: `1px solid ${isWorking ? '#232d45' : 'rgba(167,139,250,0.3)'}`,
            cursor: isWorking ? 'not-allowed' : 'pointer',
            textTransform: 'uppercase', letterSpacing: '0.1em',
          }}>↻ Try Again</button>
      </div>
    </div>
  );
}

export default function App() {
  const [token] = useState(() => {
    const q = new URLSearchParams(location.search).get('token');
    if (q) { localStorage.setItem('lotus_token', q); return q; }
    return localStorage.getItem('lotus_token') || prompt('LOTUS auth token:') || '';
  });
  const { pipeline, toast, connected, send, directAction, activity, news, claude, analysis,
          postReview, improvement, imageGen, voiceState, lastReply,
          geminiStatus, geminiTest, setGeminiTest,
          alerts, setAlerts,
          pipelineList, focusPipelineId,
          setClaude, setNews, setAnalysis, setPostReview, setImprovement, setImageGen } = useLotusSocket(token);
  const [selected, setSelected] = useState<StageKey | null>(null);
  const [input, setInput] = useState('');
  const [config, setConfig] = useState<LotusConfig | null>(null);
  const [configErr, setConfigErr] = useState<string>('');
  const [activeTab, setActiveTab] = useState<'main' | 'actions' | 'pipeline' | 'pipelines' | 'queue' | 'gallery' | 'recordings'>('main');
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [recorder, setRecorder] = useState<{active: boolean; elapsed_s: number; file: string | null}>(
    { active: false, elapsed_s: 0, file: null });
  // Auto-close overlays if Gemma stops responding. The hook dispatches
  // a `lotus:ws-activity` event on every WS message; this ref captures
  // the latest activity for the watchdog below.
  const lastActivityRef = useRef<number>(Date.now());
  useEffect(() => {
    const h = () => { lastActivityRef.current = Date.now(); };
    window.addEventListener('lotus:ws-activity', h);
    return () => window.removeEventListener('lotus:ws-activity', h);
  }, []);
  // Live-update the elapsed counter every second when recording is on
  // and listen for the WS `recorder_status` event to flip the flag.
  useEffect(() => {
    const h = (e: Event) => setRecorder((e as CustomEvent).detail);
    window.addEventListener('lotus:recorder', h);
    // Fetch current state on mount
    fetch('/api/recordings').then(r => r.json()).then(j => {
      if (j?.status) setRecorder({
        active: !!j.status.active,
        elapsed_s: j.status.elapsed_s || 0,
        file: j.status.file || null,
      });
    }).catch(() => {});
    return () => window.removeEventListener('lotus:recorder', h);
  }, []);
  useEffect(() => {
    if (!recorder.active) return;
    const t = setInterval(() => {
      setRecorder(r => ({ ...r, elapsed_s: r.elapsed_s + 1 }));
    }, 1000);
    return () => clearInterval(t);
  }, [recorder.active]);

  // Cmd+K / Ctrl+K toggles the command palette from anywhere. Also wire
  // Escape to close it. We ignore the shortcut when a text input has focus
  // so the user can still type "k" in the prompt field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement | null;
      const inInput = tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || (tgt as any).isContentEditable);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen(x => !x);
        return;
      }
      if (e.key === 'Escape' && paletteOpen) {
        setPaletteOpen(false);
      }
      if (inInput) return;
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [paletteOpen]);

  // Watchdog — auto-close any auto-opened overlays if Gemma has gone
  // silent for 45s. Prevents stale News / Analysis / ImageGen popups
  // from lingering forever when a backend call hangs or errors out.
  useEffect(() => {
    const IDLE_MS = 45_000;
    const t = setInterval(() => {
      const silent = Date.now() - lastActivityRef.current;
      if (silent < IDLE_MS) return;
      // Only auto-dismiss popups that are in a "waiting" state.
      if (analysis && analysis.status === 'working') setAnalysis(null);
      if (imageGen && imageGen.status === 'working') setImageGen(null);
      // News is user-opened; leave it alone.
    }, 5000);
    return () => clearInterval(t);
  }, [analysis, imageGen]);
  const [configOpen, setConfigOpenRaw] = useState(false);
  // Wrapper: opening the settings modal triggers a fresh gemini_status
  // broadcast so the panel reflects the live state instead of a stale snapshot.
  const setConfigOpen = (v: boolean) => {
    setConfigOpenRaw(v);
    if (v) {
      try { send({ type: 'direct_action', action: 'gemini_status' }); }
      catch {/* noop */}
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const j = await res.json();
        if (!cancelled) setConfig(j);
      } catch (e: any) {
        if (!cancelled) setConfigErr(String(e?.message || e));
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Bridge: receive config pushes (from set_projects_root) and update state.
  useEffect(() => {
    const h = (e: Event) => {
      const j = (e as CustomEvent).detail;
      if (j && j.projects_root) setConfig(j as LotusConfig);
    };
    window.addEventListener('lotus:config', h);
    return () => window.removeEventListener('lotus:config', h);
  }, []);

  // Bridge: PipelineFolderTree fires a `lotus:preview` CustomEvent on
  // thumbnail click (it's deep inside StageDetail, so a CustomEvent avoids
  // drilling the callback through multiple layers). App listens here and
  // pops the same in-dashboard image preview.
  useEffect(() => {
    const h = (e: Event) => {
      const d = (e as CustomEvent).detail || {};
      if (!d.url) return;
      setImageGen({
        status: 'done', index: 0, total: 0, id: '',
        prompt: '', section: d.section || '', improved: false,
        image_url: d.url, image_path: '',
        folder: d.section || '', filename: d.name || '',
        parent_folder: d.folder || '',
      });
    };
    window.addEventListener('lotus:preview', h);
    return () => window.removeEventListener('lotus:preview', h);
  }, [setImageGen]);

  // When a pipeline first arrives, auto-switch to the pipeline tab so the
  // user sees it. If the pipeline clears, go back to main.
  const prevPipelineId = useRef<string | null>(null);
  useEffect(() => {
    if (pipeline && pipeline.id !== prevPipelineId.current) {
      setActiveTab('pipeline');
      if (pipeline.stage === 'ask_location' && !pipeline.source_md) {
        setSelected('ask_location');
      }
      prevPipelineId.current = pipeline.id;
    } else if (!pipeline && prevPipelineId.current) {
      setActiveTab('main');
      prevPipelineId.current = null;
    }
  }, [pipeline]);

  const handleAction = (action: string, payload?: any) =>
    send({ type: action, ...(payload || {}) });
  const sendCommand = (text: string) => {
    if (!text.trim()) return;
    send({ type: 'user_command', text });
  };

  // Make the whole top bar a drag region for the Electron window. Interactive
  // children opt out with -webkit-app-region: no-drag.
  const dragStyle  = { WebkitAppRegion: 'drag' } as any;
  const noDragStyle = { WebkitAppRegion: 'no-drag' } as any;

  return (
    <div className="h-screen w-screen flex flex-col text-text overflow-hidden relative"
      style={{background: '#05070d'}}>
      {/* Global animated gradient mesh — behind every tab, under every panel */}
      <div className="absolute inset-0 pointer-events-none z-0" aria-hidden="true">
        <motion.div
          animate={{ x: [0, 60, -40, 0], y: [0, 40, -30, 0] }}
          transition={{ duration: 24, repeat: Infinity, ease: "easeInOut" }}
          className="absolute -top-40 -left-40 w-[520px] h-[520px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(0,229,255,0.16) 0%, transparent 60%)', filter: 'blur(40px)' }} />
        <motion.div
          animate={{ x: [0, -80, 50, 0], y: [0, 60, 20, 0] }}
          transition={{ duration: 30, repeat: Infinity, ease: "easeInOut" }}
          className="absolute top-1/3 right-0 w-[600px] h-[600px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(168,85,247,0.14) 0%, transparent 60%)', filter: 'blur(60px)' }} />
        <motion.div
          animate={{ x: [0, 40, -60, 0], y: [0, -40, 60, 0] }}
          transition={{ duration: 36, repeat: Infinity, ease: "easeInOut" }}
          className="absolute bottom-0 left-1/3 w-[500px] h-[500px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(77,127,255,0.11) 0%, transparent 60%)', filter: 'blur(50px)' }} />
        <div className="absolute inset-0 opacity-[0.06]"
          style={{
            backgroundImage: 'radial-gradient(circle, #fff 1px, transparent 1px)',
            backgroundSize: '24px 24px',
          }} />
      </div>

    <div className="h-full w-full flex flex-col relative z-10 overflow-hidden">
      {/* Header — centered, bold, draggable. Traffic lights sit at top-left in Electron */}
      <header style={dragStyle} className="h-[56px] shrink-0 flex items-center justify-center
                                           bg-surface border-b border-border relative z-20 px-6">
        {/* Left: connection status only (voice/speaking indicator lives
            above the command input at the bottom now). */}
        <div style={noDragStyle} className="absolute left-6 flex items-center gap-3 text-[10.5px] uppercase tracking-[0.2em] text-text-dim pl-[72px]">
          <span className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green' : 'bg-red-500'}`}
                  style={{ boxShadow: connected ? '0 0 8px #10b981' : 'none' }}/>
            {connected ? 'Connected' : 'Offline'}
          </span>
        </div>

        {/* Centered, bold, solid title */}
        <div className="flex items-baseline gap-3">
          <span className="font-display font-black text-[22px] tracking-[8px] text-text">LOTUS</span>
          <span className="text-[11px] tracking-[5px] font-bold text-accent">AGENT</span>
          <span className="text-[11px] tracking-[4px] text-text-dim">·</span>
          <span className="text-[11px] tracking-[5px] font-bold text-cyan">CONTROL CENTER</span>
        </div>

        {/* Right: destination folder + settings gear */}
        <div style={noDragStyle} className="absolute right-6 flex items-center gap-2 text-[10.5px] uppercase tracking-[0.2em] text-text-dim">
          {config && (
            <button onClick={() => setConfigOpen(true)}
              title={`Projects root: ${config.projects_root}\nClick to change`}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                background: 'rgba(0,229,255,0.08)', border: '1px solid rgba(0,229,255,0.3)',
                borderRadius: 8, padding: '5px 10px', cursor: 'pointer', color: '#e6ebf5',
              }}>
              <span style={{ fontSize: 12 }}>📁</span>
              <span className="font-mono text-text/90 normal-case tracking-normal text-[11px]">
                {config.parent_folder}/
              </span>
              <span style={{ fontSize: 9, color: '#00e5ff', letterSpacing: '0.18em' }}>EDIT</span>
            </button>
          )}
          {/* Settings gear — opens full settings / DB-backed prefs panel */}
          <button
            onClick={() => setConfigOpen(true)}
            title="Settings"
            className="w-8 h-8 rounded-lg flex items-center justify-center transition-all group relative"
            style={{
              background: 'rgba(255,255,255,0.03)',
              border: '1px solid rgba(255,255,255,0.08)',
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'rgba(0,229,255,0.1)';
              (e.currentTarget as HTMLElement).style.borderColor = 'rgba(0,229,255,0.3)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.03)';
              (e.currentTarget as HTMLElement).style.borderColor = 'rgba(255,255,255,0.08)';
            }}>
            <motion.div
              whileHover={{ rotate: 90 }}
              transition={{ duration: 0.3, ease: "easeOut" }}>
              <Settings size={15} strokeWidth={2} className="text-[#a5b4c9] group-hover:text-[#00e5ff] transition-colors" />
            </motion.div>
          </button>
        </div>
      </header>

      {/* (horizontal tab bar removed — nav lives in the vertical rail) */}

      {/* Command palette — Cmd+K / Ctrl+K opens anywhere */}
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
        recorderActive={recorder.active}
        pipelineActive={!!pipeline}
        onNavigate={(tab) => { setActiveTab(tab); setPaletteOpen(false); }}
        onAction={(a, p) => { directAction(a, p || {}); setPaletteOpen(false); }}
        onCommand={(txt) => { sendCommand(txt); setPaletteOpen(false); }} />

      {/* Always-visible REC badge — floats at top-center whenever the
          mic is recording so nobody in the room can miss it. */}
      <AnimatePresence>
        {recorder.active && (
          <motion.div
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="fixed top-3 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2 px-4 py-1.5 rounded-full pointer-events-none"
            style={{
              background: 'rgba(239,68,68,0.18)',
              border: '1px solid rgba(239,68,68,0.55)',
              boxShadow: '0 0 24px rgba(239,68,68,0.35), inset 0 0 12px rgba(239,68,68,0.15)',
              backdropFilter: 'blur(12px)',
            }}>
            <motion.div
              animate={{ opacity: [0.4, 1, 0.4], scale: [0.9, 1.15, 0.9] }}
              transition={{ duration: 1.2, repeat: Infinity }}
              className="w-2.5 h-2.5 rounded-full"
              style={{ background: '#ef4444', boxShadow: '0 0 10px #ef4444' }} />
            <span className="text-[11px] font-bold tracking-[0.3em] text-[#fca5a5] uppercase">REC</span>
            <span className="text-[11px] font-mono text-[#fecaca]/80 ml-1">
              {Math.floor(recorder.elapsed_s / 60).toString().padStart(2, '0')}:
              {(recorder.elapsed_s % 60).toString().padStart(2, '0')}
            </span>
          </motion.div>
        )}
      </AnimatePresence>

      {configOpen && (
        <ProjectsRootSettings config={config}
          onClose={() => setConfigOpen(false)}
          onSave={(p) => {
            directAction('set_projects_root', { path: p });
            setConfigOpen(false);
          }}
          onOpenFolder={() => directAction('open_projects_folder')}
          gemini={
            <GeminiKeysPanel status={geminiStatus} test={geminiTest}
              onAdd={(k) => directAction('gemini_add_key', { key: k })}
              onRemove={(preview) => directAction('gemini_remove_key', { preview })}
              onRefresh={() => directAction('gemini_status')}
              onTest={(k) => directAction('gemini_test_key', k ? { key: k } : {})}
              onClearTest={() => setGeminiTest(null)} />
          } />
      )}

      {/* Overlays: news + Gemma analysis + claude suggestion.
          When the analysis overlay is live, hide the list overlay so the
          dashboard isn't crowded — they overlap otherwise. */}
      {news && !analysis && (
        <NewsOverlay news={news} onClose={() => setNews(null)}
          onSpeak={(t) => {
            try {
              const u = new SpeechSynthesisUtterance(t);
              u.rate = 1.05; speechSynthesis.speak(u);
            } catch {/* noop */}
          }}
          onAnalyze={(item, question) => {
            // Optimistic UI — show the spinner card immediately, then let the
            // deterministic direct_action route drive the real data in.
            setAnalysis({
              title: item.title, url: item.url, analysis: '',
              status: 'working', has_article: false,
            });
            directAction('analyze_news', {
              title: item.title, url: item.url || '', question: question || '',
            });
          }} />
      )}
      {analysis && (
        <NewsAnalysisOverlay analysis={analysis}
          onClose={() => setAnalysis(null)}
          onSpeak={(t) => {
            try {
              const u = new SpeechSynthesisUtterance(t);
              u.rate = 1.05; speechSynthesis.speak(u);
            } catch {/* noop */}
          }}
          onFollowUp={(q) => sendCommand(
            `about the news "${analysis.title}" — ${q}`
          )}
          onNext={() => directAction('next_news')}
          onPrev={() => directAction('previous_news')} />
      )}
      {postReview && (
        <PostReviewOverlay review={postReview}
          onClose={() => setPostReview(null)}
          onSpeak={(t) => {
            try { const u = new SpeechSynthesisUtterance(t); u.rate = 1.05; speechSynthesis.speak(u); }
            catch {/* noop */}
          }}
          onNext={() => directAction('next_post')}
          onPrev={() => directAction('previous_post')}
          onGenerate={() => directAction('generate_post_image', { index: postReview.index + 1 })}
          onImprove={() => directAction('improve_post_prompt', { index: postReview.index + 1 })} />
      )}
      {imageGen && (
        <PostImageGenOverlay gen={imageGen}
          viewOnly={imageGen.total === 0}
          onClose={() => setImageGen(null)}
          onOpenFolder={() => directAction('open_projects_folder')}
          onApprove={() => {
            // File is already on disk. Approve just dismisses the popup and
            // logs to the activity feed.
            setImageGen(null);
          }}
          onRegenerate={() => {
            if (imageGen.total > 0) {
              directAction('generate_post_image', { index: imageGen.index + 1 });
            } else {
              // Thumbnail-only view — fire a regenerate for the current
              // cursor (or the last analysed post).
              directAction('generate_post_image');
            }
          }}
          onNextPost={() => {
            setImageGen(null);
            directAction('next_post');
          }} />
      )}
      {improvement && (
        <PostImprovementOverlay imp={improvement}
          onClose={() => setImprovement(null)}
          onAccept={() => directAction('accept_improved_prompt')}
          onAcceptAndGenerate={() => {
            // Accept the improved prompt, then fire generation in one shot.
            directAction('accept_improved_prompt');
            window.setTimeout(() => directAction('generate_post_image',
              { index: improvement.index + 1 }), 350);
          }}
          onReject={() => directAction('reject_improved_prompt')}
          onRedo={(focus) => directAction('improve_post_prompt',
            { index: improvement.index + 1, focus: focus || '' })}
          onSpeak={(t) => {
            try { const u = new SpeechSynthesisUtterance(t); u.rate = 1.05; speechSynthesis.speak(u); }
            catch {/* noop */}
          }} />
      )}
      {claude && (
        <ClaudeOverlay claude={claude}
          onClose={() => setClaude(null)}
          onRetry={() => {
            const q = window.prompt('Rephrase for Claude:', claude.query) || '';
            if (q.trim()) sendCommand(q.trim());
          }}
          onApprove={() => {
            sendCommand(`use Claude's suggestion as the plan: ${claude.response.slice(0, 400)}`);
            setClaude(null);
          }}
          onSpeak={(t) => {
            try {
              const u = new SpeechSynthesisUtterance(t);
              u.rate = 1.05; speechSynthesis.speak(u);
            } catch {/* noop */}
          }} />
      )}

      {/* Ephemeral toast under the tab bar — the header already carries the
          persistent LISTENING / THINKING / SPEAKING equaliser pill, so this
          centre strip is only for the one-line You / LOTUS message. */}
      {toast && (
        <div className="absolute left-1/2 -translate-x-1/2 z-25 pointer-events-none animate-slide-down"
             style={{ top: 104 }}>
          <div className={`flex items-center gap-3 px-4 py-2 rounded-full shadow-xl border text-[12px] max-w-[640px]
            ${toast.kind === 'cmd' ? 'bg-accent/15 border-accent' : 'bg-cyan/15 border-cyan'}`}>
            <span className={`w-1.5 h-1.5 rounded-full animate-pulse-soft
              ${toast.kind === 'cmd' ? 'bg-accent' : 'bg-cyan'}`}/>
            <span className={`uppercase tracking-wider font-semibold
              ${toast.kind === 'cmd' ? 'text-accent' : 'text-cyan'}`}>
              {toast.kind === 'cmd' ? 'You' : 'LOTUS'}
            </span>
            <span className="text-text truncate">{toast.text}</span>
          </div>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 flex min-h-0">
        {/* VERTICAL NAV RAIL (scrolls if buttons exceed height) */}
        <nav className="w-[64px] shrink-0 flex flex-col items-center py-2 gap-0.5 relative z-20 overflow-y-auto"
          style={{
            background: 'rgba(10,15,28,0.55)',
            backdropFilter: 'blur(20px) saturate(140%)',
            WebkitBackdropFilter: 'blur(20px) saturate(140%)',
            borderRight: '1px solid rgba(255,255,255,0.06)',
            scrollbarWidth: 'none',
          }}>
          {([
            { key: 'main',       label: 'Home',       Icon: Home,     tone: '#00e5ff' },
            { key: 'actions',    label: 'Actions',    Icon: Zap,      tone: '#fbbf24' },
            ...(pipeline ? [{ key: 'pipeline' as const, label: `Pipeline`, Icon: Workflow, tone: '#ff6b35' }] : []),
            { key: 'pipelines',  label: 'All Pipelines', Icon: Grid3X3, tone: '#10b981' },
            { key: 'queue',      label: 'Queue',      Icon: ListChecks, tone: '#a78bfa' },
            { key: 'gallery',    label: 'Gallery',    Icon: Library,  tone: '#a855f7' },
            { key: 'recordings', label: 'Recordings', Icon: Mic,      tone: '#ef4444' },
          ] as { key: 'main'|'actions'|'pipeline'|'pipelines'|'queue'|'gallery'|'recordings'; label: string; Icon: any; tone: string }[]).map((t) => {
            const isActive = activeTab === t.key;
            return (
              <button key={t.key}
                onClick={() => setActiveTab(t.key)}
                title={t.label}
                className="relative w-11 h-11 rounded-xl flex items-center justify-center group transition-all"
                style={{
                  background: isActive ? `${t.tone}22` : 'transparent',
                  boxShadow: isActive ? `0 0 20px ${t.tone}44, inset 0 0 0 1px ${t.tone}66` : 'none',
                }}>
                {isActive && (
                  <motion.div layoutId="nav-bar"
                    className="absolute -left-4 top-2 bottom-2 w-[3px] rounded-r"
                    style={{ background: t.tone, boxShadow: `0 0 12px ${t.tone}` }} />
                )}
                <t.Icon size={18} strokeWidth={2}
                  color={isActive ? t.tone : '#7f8aa3'}
                  className="transition-colors group-hover:!text-white" />
                {/* Label tooltip */}
                <span className="absolute left-14 px-2 py-1 rounded-md text-[11px] font-medium whitespace-nowrap pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity"
                  style={{ background: 'rgba(10,15,28,0.9)', color: '#e6ebf5', border: '1px solid rgba(255,255,255,0.08)' }}>
                  {t.label}
                </span>
                {t.key === 'pipeline' && pipeline && (
                  <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full animate-pulse"
                    style={{ background: t.tone, boxShadow: `0 0 6px ${t.tone}` }} />
                )}
              </button>
            );
          })}
          <div className="flex-1" />
          {/* Mic / REC toggle */}
          <button
            onClick={() => directAction(recorder.active ? 'rec_stop' : 'rec_start')}
            title={recorder.active ? 'Stop recording' : 'Start mic recording'}
            className="relative w-11 h-11 rounded-xl flex items-center justify-center group transition-all mb-1"
            style={{
              background: recorder.active ? 'rgba(239,68,68,0.18)' : 'transparent',
              boxShadow: recorder.active ? '0 0 20px rgba(239,68,68,0.4), inset 0 0 0 1px rgba(239,68,68,0.6)' : 'none',
            }}>
            {recorder.active ? (
              <>
                <motion.span
                  animate={{ opacity: [0.5, 1, 0.5], scale: [0.9, 1.1, 0.9] }}
                  transition={{ duration: 1.2, repeat: Infinity }}
                  className="w-3 h-3 rounded-full" style={{ background: '#ef4444', boxShadow: '0 0 12px #ef4444' }} />
              </>
            ) : (
              <Mic size={18} strokeWidth={2} className="text-[#7f8aa3] group-hover:text-white transition-colors" />
            )}
            <span className="absolute left-14 px-2 py-1 rounded-md text-[11px] font-medium whitespace-nowrap pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity"
              style={{ background: 'rgba(10,15,28,0.9)', color: '#e6ebf5', border: '1px solid rgba(255,255,255,0.08)' }}>
              {recorder.active ? 'Stop REC' : 'Record mic'}
            </span>
          </button>
          {/* Gemma Brain — in-rail, never opens a separate window.
              Icon gets an animated electric-wire halo when Gemma thinks. */}
          <div className="relative w-11 h-11 rounded-xl flex items-center justify-center group mb-1"
            title={voiceState === 'thinking' ? 'Gemma thinking…' : 'Gemma'}
            style={{
              background: voiceState === 'thinking'
                ? 'rgba(168,85,247,0.18)' : 'transparent',
              boxShadow: voiceState === 'thinking'
                ? '0 0 20px rgba(168,85,247,0.4), inset 0 0 0 1px rgba(168,85,247,0.55)' : 'none',
            }}>
            {/* Brain icon + animated electric wire overlay */}
            <div className="relative">
              <Brain size={18} strokeWidth={2}
                color={voiceState === 'thinking' ? '#a855f7' :
                       voiceState === 'speaking' ? '#ff6b35' :
                       voiceState === 'listening' ? '#00e5ff' : '#7f8aa3'}
                className="transition-colors group-hover:!text-white" />
              {(voiceState === 'thinking' ||
                voiceState === 'listening' || voiceState === 'speaking') && (
                <svg viewBox="0 0 24 24" className="absolute -inset-1 w-[26px] h-[26px] pointer-events-none">
                  {/* Outer spark ring with flowing dashes */}
                  <circle cx="12" cy="12" r="11"
                    fill="none"
                    stroke={voiceState === 'thinking' ? '#a855f7' :
                            voiceState === 'speaking' ? '#ff6b35' : '#00e5ff'}
                    strokeWidth="0.6"
                    strokeDasharray="1.6 1.2"
                    strokeOpacity="0.75"
                    style={{
                      animation: 'brainwire-dash 1.4s linear infinite',
                      filter: 'drop-shadow(0 0 1px currentColor)',
                    }} />
                  {/* 3 spark dots orbiting */}
                  {[0, 120, 240].map((deg, i) => (
                    <circle key={i} cx="12" cy="1" r="0.9"
                      fill={voiceState === 'thinking' ? '#c4b5fd' :
                            voiceState === 'speaking' ? '#fdba74' : '#7dd3fc'}
                      style={{
                        transformOrigin: '12px 12px',
                        animation: `brainwire-spin 2.0s linear infinite`,
                        animationDelay: `${i * -0.65}s`,
                        transform: `rotate(${deg}deg)`,
                      }} />
                  ))}
                </svg>
              )}
            </div>
            <span className="absolute left-14 px-2 py-1 rounded-md text-[11px] font-medium whitespace-nowrap pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity"
              style={{ background: 'rgba(10,15,28,0.9)', color: '#e6ebf5', border: '1px solid rgba(255,255,255,0.08)' }}>
              Gemma{voiceState === 'thinking' ? ' · thinking' :
                   voiceState === 'speaking' ? ' · speaking' :
                   voiceState === 'listening' ? ' · listening' : ''}
            </span>
          </div>
          {/* Cancel / Emergency-Stop moved into the Pipeline dashboard's
              top-right control panel (see PipelineCanvas). Nav rail stays
              focused on navigation. */}
        </nav>

        <main className="flex-1 relative min-h-0">
          {activeTab === 'main' ? (
            <HeroView voiceState={voiceState} lastReply={lastReply} />
          ) : activeTab === 'actions' ? (
            <MainDashboard config={config} pipeline={pipeline} activity={activity}
              onSendCommand={sendCommand} onDirectAction={directAction}
              onOpenSettings={() => setConfigOpen(true)}
              onPreviewImage={(img) => setImageGen({
                status: 'done',
                index: 0, total: 0,
                id: '',
                prompt: '',
                section: img.section,
                improved: false,
                image_url: img.url,
                image_path: '',
                folder: img.section,
                filename: img.name,
                parent_folder: img.folder,
              })}
              voiceState={voiceState} lastReply={lastReply} />
          ) : activeTab === 'pipelines' ? (
            <PipelinesOverview
              liveIds={new Set(pipelineList.map(p => p.id))}
              onFocus={(id) => {
                handleAction('pipeline_focus', { id });
                setActiveTab('pipeline');
              }}
              onReopen={(id) => {
                // Reactivate a finished pipeline into the live registry, then
                // jump to the Pipeline view where per-frame Regenerate lives.
                directAction('reopen_pipeline', { id });
                setActiveTab('pipeline');
              }}
              onCreate={(name) => {
                directAction('start_pipeline', { name });
                // After creating, switch to the Pipeline tab so the user
                // can immediately drop the source MD file onto the new
                // pipeline's ask_location stage.
                setActiveTab('pipeline');
              }}
              onDelete={(id) => {
                directAction('delete_pipeline', { id });
              }}
              onBackfill={(id) => {
                directAction('backfill_pipeline_folder', { id });
              }} />
          ) : activeTab === 'queue' ? (
            <QueueView />
          ) : activeTab === 'gallery' ? (
            <GalleryTab />
          ) : activeTab === 'recordings' ? (
            <RecordingsTab recorder={recorder}
              onStart={() => directAction('rec_start')}
              onStop={() => directAction('rec_stop')}
              onDelete={(name) => directAction('rec_delete', { name })} />
          ) : pipeline ? (
            <ReactFlowProvider>
              <PipelineCanvas pipeline={pipeline} selected={selected}
                onSelect={(k) => setSelected(prev => prev === k ? null : k)}
                onCancel={() => directAction('pipeline_cancel')}
                onEmergencyStop={() => directAction('emergency_stop')} />
            </ReactFlowProvider>
          ) : (
            <div className="h-full flex flex-col items-center justify-center text-text-dim text-[13px]">
              No active pipeline.
            </div>
          )}

          {activeTab === 'pipeline' && (
            <>
              {/* Multi-pipeline tab bar — one chip per active run. Click to
                  switch focus (sends pipeline_focus WS action). Cancel (×)
                  removes a pipeline from the registry. */}
              {pipelineList.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6,
                               marginBottom: 10, alignItems: 'center' }}>
                  {pipelineList.map((p) => {
                    const isFocus = p.id === focusPipelineId;
                    const stageColor = p.stage === 'done' ? '#10b981'
                                     : p.paused         ? '#fbbf24'
                                     : p.cancelled      ? '#7f8aa3'
                                     : p.stage === 'generating' ? '#a78bfa'
                                     : p.stage === 'review_frames' ? '#00e5ff'
                                     : '#7f8aa3';
                    return (
                      <div key={p.id}
                        onClick={() => { if (!isFocus) handleAction('pipeline_focus', { id: p.id }); }}
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: 6,
                          padding: '5px 10px', borderRadius: 999,
                          background: isFocus ? withAlpha(stageColor, 0.18) : '#0a0f1c',
                          border: `1px solid ${isFocus ? withAlpha(stageColor, 0.55) : '#232d45'}`,
                          cursor: isFocus ? 'default' : 'pointer',
                          fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
                          transition: 'background 0.15s, border-color 0.15s',
                        }}
                        title={`${p.frame_count} frames · stage=${p.stage}${p.paused ? ' · paused' : ''}`}>
                        <span style={{ width: 6, height: 6, borderRadius: 999,
                                        background: stageColor,
                                        boxShadow: isFocus ? `0 0 6px ${stageColor}` : 'none' }} />
                        <span style={{ color: isFocus ? '#e6ebf5' : '#dbe3f0',
                                        fontWeight: isFocus ? 700 : 500 }}>
                          {p.name}
                        </span>
                        <span style={{ fontSize: 9, color: '#7f8aa3',
                                        letterSpacing: '0.08em', fontWeight: 600 }}>
                          {p.frame_count}f · {p.stage}
                        </span>
                        <button onClick={(e) => {
                          e.stopPropagation();
                          if (confirm(`Cancel pipeline "${p.name}"?`)) {
                            handleAction('pipeline_cancel', { id: p.id });
                          }
                        }}
                          title="Cancel this pipeline"
                          style={{
                            background: 'transparent', border: 'none',
                            color: '#7f8aa3', cursor: 'pointer',
                            fontSize: 13, padding: 0, lineHeight: 1,
                            marginLeft: 2,
                          }}>×</button>
                      </div>
                    );
                  })}
                </div>
              )}
              <StageDetail pipeline={pipeline} selected={selected} config={config}
                onAction={handleAction} onClose={() => setSelected(null)}
                alerts={alerts}
                onDismissAlert={(id) => setAlerts(prev => prev.filter(x => x.id !== id))} />
            </>
          )}
        </main>

        {activeTab === 'pipeline' && (
          <MetricsSidebar pipeline={pipeline} config={config} configErr={configErr}
            activity={activity} connected={connected}
            onAction={handleAction} />
        )}
      </div>

      <div className="shrink-0 bg-surface border-t border-border relative z-20">
        {/* Thin voice pill only when NOT on the main dashboard — on main we
            already show the big right-rail VoiceAnimationBox, so this would
            duplicate. On the Pipeline tab it's still useful. */}
        {activeTab !== 'main' && (
          <div className="h-[34px] flex items-center justify-center border-b border-border-soft"
               style={{ background: 'linear-gradient(90deg,#0f1525,#141a28,#0f1525)' }}>
            <VoiceStatus state={voiceState} lastReply={lastReply} />
          </div>
        )}
        <footer className="h-[60px] flex items-center gap-3 px-5">
          <input
            value={input} onChange={(e) => setInput(e.target.value)}
            placeholder='Type a command, e.g. "start pipeline Demo"'
            className="flex-1 bg-canvas border border-border-soft rounded-lg px-4 py-2.5 text-[13px]
                       text-text placeholder:text-text-dim outline-none focus:border-accent"
            onKeyDown={(e) => {
              if (e.key === 'Enter') { sendCommand(input); setInput(''); }
            }}
          />
          <button
            className="bg-accent hover:bg-accent-strong text-white rounded-lg px-6 py-2.5
                       font-display font-bold text-[11px] tracking-[3px] transition-all hover:-translate-y-px shadow-lg"
            onClick={() => { sendCommand(input); setInput(''); }}
          >
            SEND
          </button>
        </footer>
      </div>
    </div>
    </div>
  );
}
