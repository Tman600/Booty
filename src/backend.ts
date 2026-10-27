import { callable } from "@decky/api";
import { useEffect, useState } from "react";

export interface BootEntry {
  id: string;
  label: string;
  name: string;
  kind: "steamos" | "windows" | "os" | "firmware" | "installer";
  current: boolean;
  switchable: boolean;
}

export interface Settings {
  show_in_power_menu: boolean;
  keep_steamos_default: boolean;
  hidden: string[];
  names: Record<string, string>;
}

export interface CatalogItem {
  id: string;
  name: string;
  blurb: string;
  write: "raw" | "windows";
}

export interface Drive {
  path: string;
  size: number;
  kind: string;
  model: string;
  labels: string[];
  mounts: string[];
  has_games: boolean;
}

export interface Job {
  type: "download" | "write" | "prepare" | "backup" | "restore";
  phase: string;
  done: number;
  total: number;
  message: string;
  file?: string;
  verified?: boolean;
  device?: string;
  mode?: string;
}

export const getEntries = callable<[], { entries: BootEntry[]; current: string }>("get_entries");
export const getSettings = callable<[], Settings>("get_settings");
export const setSetting = callable<[key: keyof Settings, value: unknown], Settings>("set_setting");
export const switchTo = callable<[id: string], boolean>("switch_to");
export const getCatalog = callable<[], CatalogItem[]>("get_catalog");
export const startDownload = callable<[osId: string, url: string | null], Job>("start_download");
export const getJob = callable<[], Job | null>("get_job");
export const cancelJob = callable<[], boolean>("cancel_job");
export const clearJob = callable<[], boolean>("clear_job");
export const listIsos = callable<[], { path: string; name: string; size: number }[]>("list_isos");
export const detectIso = callable<[path: string], "raw" | "windows">("detect_iso");
export const listDrives = callable<[], Drive[]>("list_drives");
export const startWrite = callable<
  [iso: string, device: string, size: number, mode: string, avoid: string | null],
  Job
>("start_write");

export interface InstallTarget extends Drive {
  modes: ("keep" | "erase")[];
  max_keep: number;
  max_erase: number;
  used: number;
}
export const listInstallTargets = callable<[], InstallTarget[]>("list_install_targets");
export const startPrepare = callable<
  [device: string, size: number, mode: "keep" | "erase", osBytes: number, installer: string | null],
  Job
>("start_prepare");
export const bootInstaller = callable<[device: string], boolean>("boot_installer");

export interface ReturnButtonResult {
  name: string;
  partition: string;
  ok: boolean;
  detail: string;
}
export const addReturnButtons = callable<[], ReturnButtonResult[]>("add_return_buttons");

// Boot entries and settings are cached here so the power menu can render them
// synchronously when Steam opens it.
type State = { entries: BootEntry[]; settings: Settings | null };
let state: State = { entries: [], settings: null };
const listeners = new Set<() => void>();

export function getState() {
  return state;
}

export async function refresh() {
  const [{ entries }, settings] = await Promise.all([getEntries(), getSettings()]);
  state = { entries, settings };
  listeners.forEach((l) => l());
  return state;
}

export async function updateSetting(key: keyof Settings, value: unknown) {
  state = { ...state, settings: await setSetting(key, value) };
  listeners.forEach((l) => l());
}

export function useBootyState() {
  const [s, setS] = useState(state);
  useEffect(() => {
    const l = () => setS(state);
    listeners.add(l);
    refresh().catch(console.error);
    return () => {
      listeners.delete(l);
    };
  }, []);
  return s;
}

export function formatBytes(n: number) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1000)));
  return `${(n / 1000 ** i).toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

export interface DriveContents {
  parts: { path: string; label: string; fstype: string; used: number; files: number; top: string[] }[];
  used: number;
  files: number;
  free_home: number;
}
export interface Backup {
  name: string;
  model: string;
  kind: string;
  size: number;
  bytes: number;
  files: number;
  created: string;
}
export const inspectDrive = callable<[device: string], DriveContents>("inspect_drive");
export const startBackup = callable<[device: string, size: number], Job>("start_backup");
export const listBackups = callable<[], Backup[]>("list_backups");
export const startRestore = callable<[name: string, device: string, size: number], Job>("start_restore");

export const repairEntries = callable<
  [],
  { found: { name: string; partition: string; loader: string }[]; created: string[]; tidied: string[] }
>("repair_entries");
