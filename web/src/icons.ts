import {
  Airplay, AudioWaveform, Cast, Check, ChevronDown, ChevronRight, ChevronUp, Download, FileText, Link2, Moon,
  Maximize2, Projector, RadioTower, Server, SlidersHorizontal, Smartphone, Square, Trash2,
  Clock3, FolderOpen, LockKeyhole, Minimize2, Monitor, Pause, Play, Settings, Share2, Speaker, SquareTerminal, Sun, Tv, Undo2, User, Volume2, VolumeX, X,
} from "lucide";
import { siXiaomi } from "simple-icons";

export type IconName = "airplay" | "antenna" | "speaker" | "settings" |
  "person" | "terminal" | "sun" | "moon" | "appearance" | "wave" | "check" |
  "play" | "pause" | "close" | "cast" | "link" | "clock" | "topology" |
  "tv" | "loudspeaker" | "projector" | "chevron" | "chevron-up" | "chevron-down" | "minimize" | "maximize" | "mute" | "lock" | "folder" |
  "download" | "undo" | "file" | "trash" | "phone" | "server" | "square" | "sliders";

const lucideIcons: Record<IconName, unknown> = {
  airplay: Airplay, antenna: RadioTower, speaker: Volume2,
  settings: Settings, person: User, terminal: SquareTerminal,
  sun: Sun, moon: Moon, appearance: Monitor,
  wave: AudioWaveform, check: Check, play: Play, pause: Pause, close: X,
  cast: Cast, link: Link2, clock: Clock3, topology: Share2,
  tv: Tv, loudspeaker: Speaker, projector: Projector, chevron: ChevronRight,
  "chevron-up": ChevronUp, "chevron-down": ChevronDown,
  minimize: Minimize2, maximize: Maximize2,
  square: Square,
  sliders: SlidersHorizontal,
  mute: VolumeX,
  lock: LockKeyhole,
  folder: FolderOpen,
  download: Download,
  undo: Undo2,
  file: FileText,
  trash: Trash2,
  phone: Smartphone,
  server: Server,
};

export function icon(name: IconName, className = "symbol"): string {
  const [, baseAttrs, children] = lucideIcons[name] as [string, Record<string, unknown>, unknown[]];
  const attrs = { ...baseAttrs, class: className, "aria-hidden": "true", width: undefined, height: undefined, "stroke-width": 1.9 };
  return `<svg${renderAttrs(attrs)}>${children.map(renderNode).join("")}</svg>`;
}

function renderNode(value: unknown): string {
  const [tag, attrs, children = []] = value as [string, Record<string, unknown>, unknown[]?];
  return `<${tag}${renderAttrs(attrs)}>${children.map(renderNode).join("")}</${tag}>`;
}

function renderAttrs(attrs: Record<string, unknown>): string {
  return Object.entries(attrs).filter(([, value]) => value !== undefined).map(([key, value]) => ` ${key}="${String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`).join("");
}

export function brandMark(): string {
  return `<img class="brand-symbol" src="icons/micast.svg" alt="" aria-hidden="true">`;
}

export function brandIcon(brand: "xiaomi", className = "brand-device-symbol"): string {
  const source = brand === "xiaomi" ? siXiaomi : siXiaomi;
  return `<svg class="${className}" viewBox="0 0 24 24" aria-label="${source.title}" role="img" fill="currentColor"><path d="${source.path}"/></svg>`;
}
