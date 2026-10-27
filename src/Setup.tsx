import { FileSelectionType, openFilePicker, toaster } from "@decky/api";
import { Card, Progress, Section, Text, useJob } from "./ui";
import {
  ConfirmModal,
  DialogBody,
  DialogButton,
  Focusable,
  Navigation,
  showModal,
  SliderField,
  TextField,
} from "@decky/ui";
import { ReactNode, useEffect, useState } from "react";
import {
  bootInstaller,
  cancelJob,
  CatalogItem,
  clearJob,
  detectIso,
  Drive,
  formatBytes,
  getCatalog,
  inspectDrive,
  InstallTarget,
  listDrives,
  listInstallTargets,
  startBackup,
  startDownload,
  startPrepare,
  startWrite,
} from "./backend";

type Step = "choose" | "windows" | "target" | "download" | "drive" | "backup" | "write" | "prepare" | "ready";
type Image = { path: string; mode: "raw" | "windows"; name: string };
type Pending = { kind: "download"; item: CatalogItem; url: string | null } | { kind: "file" };
type Plan = { target: InstallTarget; mode: "keep" | "erase"; bytes: number };

const MS_PAGE = "https://www.microsoft.com/software-download/windows11";
const GB = 1000 ** 3;
// Smallest sensible install per OS, in GB.
const MIN_GB: Record<string, number> = { bazzite: 64, "fedora-kde": 30, ubuntu: 30, windows: 64 };

function installerHint(osId: string | undefined, isWindows: boolean, gb: number) {
  if (isWindows || osId === "windows")
    return `Choose "Custom", select the "Unallocated Space" on that drive (about ${gb} GB), then Next.`;
  if (osId === "bazzite" || osId === "fedora-kde")
    return `Open "Installation Destination", select that drive, leave Storage Configuration on Automatic, and press Done. It installs into the free space.`;
  if (osId === "ubuntu")
    return `Choose "Install alongside" if it's offered; otherwise pick "Manual installation" and install into the ${gb} GB of free space.`;
  return `Install into the ${gb} GB of free space on that drive.`;
}

export function Setup() {
  const [step, setStep] = useState<Step>("choose");
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [chosen, setChosen] = useState<CatalogItem | null>(null);
  const [image, setImage] = useState<Image | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [targets, setTargets] = useState<InstallTarget[]>([]);
  const [sel, setSel] = useState<InstallTarget | null>(null);
  const [mode, setMode] = useState<"keep" | "erase">("keep");
  const [gb, setGb] = useState(64);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [drives, setDrives] = useState<Drive[]>([]);
  const [drive, setDrive] = useState<Drive | null>(null);
  const [link, setLink] = useState("");
  const job = useJob();

  useEffect(() => {
    getCatalog().then(setCatalog);
  }, []);

  const fail = (e: any) => toaster.toast({ title: "Booty", body: String(e?.message ?? e) });
  const minGb = MIN_GB[chosen?.id ?? (image?.mode === "windows" ? "windows" : "")] ?? 30;

  const prepare = () => {
    if (!plan) return setStep("ready");
    startPrepare(plan.target.path, plan.target.size, plan.mode, plan.bytes, drive?.path ?? null).then(
      () => setStep("prepare"),
      fail
    );
  };

  // Advance when a background job finishes.
  useEffect(() => {
    if (!job) return;
    if (job.type === "download" && job.phase === "done" && step === "download" && job.file) {
      setImage({ path: job.file, mode: chosen?.write ?? "raw", name: chosen?.name ?? "OS" });
      clearJob();
      setStep("drive");
    } else if (job.type === "backup" && job.phase === "done" && step === "backup") {
      toaster.toast({ title: "Booty", body: job.message });
      clearJob();
      if (drive) write(drive);
    } else if (job.type === "write" && job.phase === "done" && step === "write") {
      clearJob();
      prepare();
    } else if (job.type === "prepare" && job.phase === "done" && step === "prepare") {
      clearJob();
      setStep("ready");
    } else if (["error", "cancelled"].includes(job.phase)) {
      toaster.toast({ title: "Booty", body: job.message });
      clearJob();
      setStep(job.type === "download" ? "choose" : job.type === "write" || job.type === "backup" ? "drive" : "target");
    }
  }, [job]);

  useEffect(() => {
    if (step === "drive") listDrives().then(setDrives);
    if (step === "target") listInstallTargets().then(setTargets);
  }, [step]);

  const continueAfterTarget = (p: Plan | null) => {
    setPlan(p);
    if (pending?.kind === "download") {
      startDownload(pending.item.id, pending.url).then(() => setStep("download"), fail);
    } else {
      setStep("drive");
    }
  };

  const chooseOs = (item: CatalogItem, url: string | null) => {
    setChosen(item);
    setPending({ kind: "download", item, url });
    setStep("target");
  };

  const pickFile = async () => {
    try {
      const res = await openFilePicker(FileSelectionType.FILE, "/home/deck/Downloads", true, true, undefined, ["iso"]);
      const isoMode = await detectIso(res.realpath);
      setChosen(isoMode === "windows" ? catalog.find((c) => c.id === "windows") ?? null : null);
      setImage({ path: res.realpath, mode: isoMode, name: isoMode === "windows" ? "Windows" : res.path.split("/").pop() || "OS" });
      setPending({ kind: "file" });
      setStep("target");
    } catch (e) {
      if (e) console.log("[Booty] file picker closed", e);
    }
  };

  const selectTarget = (t: InstallTarget) => {
    const m = t.modes[0];
    const max = Math.floor((m === "keep" ? t.max_keep : t.max_erase) / GB);
    setSel(t);
    setMode(m);
    setGb(Math.max(Math.min(max, minGb * 2), Math.min(minGb, max)));
  };

  const confirmTarget = () => {
    if (!sel) return;
    const p: Plan = { target: sel, mode, bytes: gb * GB };
    const desc =
      mode === "keep"
        ? `Booty will shrink the games partition on your ${sel.kind} to free up ${gb} GB for the new OS. Your games stay. ` +
          `This happens after the installer is written, and takes a few minutes. Keep the Deck plugged in.`
        : `Everything on ${sel.model} (${formatBytes(sel.size)}) will be erased. The new OS gets ${gb} GB` +
          (sel.size - gb * GB > 4 * GB ? `, and the rest becomes an empty storage partition.` : `.`);
    showModal(
      <ConfirmModal
        strTitle={mode === "keep" ? `Free up ${gb} GB?` : `Erase ${sel.model}?`}
        strDescription={desc}
        strOKButtonText={mode === "keep" ? "Continue" : "Erase it"}
        bDestructiveWarning={mode === "erase" || sel.has_games}
        onOK={() => continueAfterTarget(p)}
      />
    );
  };

  const write = (d: Drive) =>
    startWrite(image!.path, d.path, d.size, image!.mode, plan?.target.path ?? null).then(() => setStep("write"), fail);

  const confirmDrive = async (d: Drive) => {
    setDrive(d);
    let contents = null;
    try {
      contents = await inspectDrive(d.path);
    } catch (e) {
      console.log("[Booty] couldn't look inside the drive", e);
    }
    const what = `the ${image?.name} installer`;
    if (contents && contents.files > 0) {
      const fits = contents.used < contents.free_home - 5 * GB;
      showModal(
        <ConfirmModal
          strTitle={`${d.model} has ${contents.files} files on it`}
          strDescription={
            `They take up ${formatBytes(contents.used)}` +
            (contents.parts[0]?.top.length ? ` (${contents.parts[0].top.slice(0, 4).join(", ")}...)` : "") +
            `. Writing ${what} erases them. ` +
            (fits
              ? `Booty can copy them to "Booty Backups" on the Deck first, and put them back later from Booty's panel.`
              : `There isn't enough free space on the Deck to back them up (${formatBytes(contents.free_home)} free).`)
          }
          strOKButtonText={fits ? "Back up, then write" : "Erase and write"}
          strMiddleButtonText={fits ? "Erase without backup" : undefined}
          bDestructiveWarning={!fits}
          onMiddleButton={fits ? () => write(d) : undefined}
          onOK={() => (fits ? startBackup(d.path, d.size).then(() => setStep("backup"), fail) : write(d))}
        />
      );
      return;
    }
    showModal(
      <ConfirmModal
        strTitle={`Erase ${d.model}?`}
        strDescription={
          `Everything on this ${d.kind} (${formatBytes(d.size)}) will be deleted and replaced with ${what}.` +
          (d.has_games ? " It has Steam games on it, and they'll be erased too." : "")
        }
        strOKButtonText="Erase and write"
        bDestructiveWarning={true}
        onOK={() => write(d)}
      />
    );
  };

  let content: ReactNode;
  if (step === "choose") {
    content = (
      <>
        <Section title="How it works">
          <Text>
            Pick an operating system and where it should go. Booty downloads it, checks it against the official
            checksum, writes the installer to a USB stick, makes room on the drive you picked, and restarts into the
            installer. Once it's installed, "Switch to ..." shows up in the power menu and in Quick Access, and SteamOS
            stays the default.
          </Text>
        </Section>
        <Section title="Choose an operating system">
          {catalog.map((c) => (
            <Card key={c.id} title={c.name} body={c.blurb}
              onClick={() => (c.id === "windows" ? (setChosen(c), setStep("windows")) : chooseOs(c, null))} />
          ))}
          <Card title="Use an ISO file already on the Deck" body="Any Linux ISO or a Windows 11 ISO." onClick={pickFile} />
        </Section>
      </>
    );
  } else if (step === "windows") {
    const win = catalog.find((c) => c.id === "windows")!;
    content = (
      <Section title="Windows 11">
        <Text>
          On your phone or PC, open microsoft.com/software-download/windows11, choose "Download Windows 11 Disk Image
          (ISO) for x64 devices", pick a language, and copy the 64-bit download link. The link works for 24 hours.
          Paste it below; press Steam + X for the keyboard.
        </Text>
        <TextField label="Download link" value={link} onChange={(e) => setLink(e.target.value)} />
        <Focusable style={{ display: "flex", gap: "8px", marginTop: "12px" }} flow-children="horizontal">
          <DialogButton disabled={!link.startsWith("https://")} onClick={() => chooseOs(win, link.trim())}>
            Next
          </DialogButton>
          <DialogButton onClick={pickFile}>Use an ISO file instead</DialogButton>
          <DialogButton onClick={() => Navigation.NavigateToExternalWeb(MS_PAGE)}>Open Microsoft's page</DialogButton>
          <DialogButton onClick={() => setStep("choose")}>Back</DialogButton>
        </Focusable>
      </Section>
    );
  } else if (step === "target") {
    const max = sel ? Math.floor((mode === "keep" ? sel.max_keep : sel.max_erase) / GB) : 0;
    content = (
      <Section title={`Where should ${chosen?.name ?? image?.name ?? "it"} go?`}>
        <Text>
          Booty sets aside the space you choose, so the installer only has to use it. The installer itself needs a
          separate USB stick, which you'll pick next.
        </Text>
        {targets.map((t) => (
          <Card key={t.path} title={`${t.model} (${formatBytes(t.size)})${sel?.path === t.path ? "  - selected" : ""}`}
            body={t.modes.includes("keep")
              ? `${t.kind}. Up to ${Math.floor(t.max_keep / GB)} GB can be freed and your games kept (${formatBytes(t.used)} used).`
              : `${t.kind}. Gets erased; up to ${Math.floor(t.max_erase / GB)} GB for the OS.`}
            onClick={() => selectTarget(t)} />
        ))}
        <Card title="Internal drive" disabled={true}
          body="Not automatic: SteamOS's home partition can only shrink while SteamOS is off, and a failed resize can lose your games and saves. Use the SD card or a USB SSD." />
        {targets.length === 0 && <Text>No SD card or USB drive found. Insert one, then refresh.</Text>}

        {sel && (
          <>
            {sel.modes.length > 1 && (
              <Focusable style={{ display: "flex", gap: "8px", marginBottom: "8px" }} flow-children="horizontal">
                <DialogButton onClick={() => selectTarget({ ...sel, modes: ["keep", "erase"] })}
                  style={{ opacity: mode === "keep" ? 1 : 0.6 }}>Keep my games</DialogButton>
                <DialogButton onClick={() => { setMode("erase"); setGb(Math.min(gb, Math.floor(sel.max_erase / GB))); }}
                  style={{ opacity: mode === "erase" ? 1 : 0.6 }}>Erase the whole drive</DialogButton>
              </Focusable>
            )}
            {max >= minGb ? (
              <SliderField label={`Space for ${chosen?.name ?? image?.name ?? "the OS"}`} value={gb} min={minGb} max={max}
                step={1} showValue={true} valueSuffix=" GB" editableValue={true}
                description={`At least ${minGb} GB is recommended.`} onChange={(v) => setGb(Math.round(v))} />
            ) : (
              <Text>This drive doesn't have enough room. At least {minGb} GB is needed.</Text>
            )}
          </>
        )}
        <Focusable style={{ display: "flex", gap: "8px", marginTop: "12px" }} flow-children="horizontal">
          <DialogButton disabled={!sel || max < minGb} onClick={confirmTarget}>Next</DialogButton>
          <DialogButton onClick={() => continueAfterTarget(null)}>Skip, I'll choose in the installer</DialogButton>
          <DialogButton onClick={() => listInstallTargets().then(setTargets)}>Refresh</DialogButton>
          <DialogButton onClick={() => setStep("choose")}>Back</DialogButton>
        </Focusable>
      </Section>
    );
  } else if (step === "download") {
    content = (
      <Section title={`Downloading ${chosen?.name ?? ""}`}>
        <Progress job={job} onCancel={() => cancelJob()} />
      </Section>
    );
  } else if (step === "drive") {
    const usable = drives.filter((d) => d.path !== plan?.target.path);
    content = (
      <Section title={`Write the ${image?.name} installer to...`}>
        <Text>
          Pick a USB stick (8 GB or bigger). It gets erased. The Deck's own drive
          {plan ? ` and ${plan.target.model}, where the OS is going,` : ""} aren't listed.
        </Text>
        {usable.map((d) => (
          <Card key={d.path} title={`${d.model} (${formatBytes(d.size)})`}
            body={`${d.kind}  ${d.path}${d.labels.length ? "  " + d.labels.join(", ") : ""}`}
            warn={d.has_games ? "Has your Steam games on it" : undefined}
            onClick={() => confirmDrive(d)} />
        ))}
        {usable.length === 0 && (
          <Text>No spare drive found. Plug in a USB stick (a USB-C stick or a dock works), then refresh.</Text>
        )}
        <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
          <DialogButton onClick={() => listDrives().then(setDrives)}>Refresh</DialogButton>
          <DialogButton onClick={() => setStep("target")}>Back</DialogButton>
        </Focusable>
      </Section>
    );
  } else if (step === "backup") {
    content = (
      <Section title={`Backing up ${drive?.model ?? "the drive"}`}>
        <Text>Your files are being copied to "Booty Backups" on the Deck. Nothing is erased until this finishes.</Text>
        <Progress job={job} onCancel={() => cancelJob()} />
      </Section>
    );
  } else if (step === "write") {
    content = (
      <Section title={`Writing to ${drive?.model ?? ""}`}>
        <Progress job={job} onCancel={() => cancelJob()} />
      </Section>
    );
  } else if (step === "prepare") {
    content = (
      <Section title={`Making room on ${plan?.target.model ?? ""}`}>
        <Text>Don't turn the Deck off or remove the card until this finishes.</Text>
        <Progress job={job} />
      </Section>
    );
  } else {
    const isWin = image?.mode === "windows";
    const gbPlanned = plan ? Math.round(plan.bytes / GB) : 0;
    content = (
      <Section title="The installer is ready">
        <Text>
          Restarting now boots the {image?.name} installer once.
          {plan ? (
            <>
              <br />When it asks where to install, pick {plan.target.model} ({formatBytes(plan.target.size)}).{" "}
              {installerHint(chosen?.id, isWin, gbPlanned)}
            </>
          ) : (
            <>
              <br />When it asks where to install, use another drive like an SD card or a USB SSD. The Deck's internal
              drive only works if you shrink SteamOS's home partition first; back up your games and saves before
              trying that.
            </>
          )}
          {isWin && (
            <>
              <br />After Windows installs, get Valve's Windows drivers for the Deck (search "Steam Deck Windows
              resources") so Wi-Fi, sound and the controls work.
            </>
          )}
          <br />When the installer finishes, unplug the USB stick and restart. To get back to SteamOS, hold Volume
          Down and press Power, then choose SteamOS. If the new OS shows a boot menu, its "SteamOS Holo" entries won't
          work; pick the new OS instead.
          <br />The next time SteamOS starts, Booty finishes the setup: SteamOS stays the default, the new OS's boot
          menu is hidden and gets a working SteamOS entry, and "Switch to ..." appears in the power menu. Then press
          "Add Back to SteamOS buttons" for a one-tap way home from the other OS.
        </Text>
        <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
          <DialogButton onClick={() => bootInstaller(drive!.path).catch(fail)}>Restart into the installer</DialogButton>
          <DialogButton onClick={() => Navigation.NavigateBack()}>Later</DialogButton>
        </Focusable>
      </Section>
    );
  }

  return (
    <div style={{ marginTop: "40px", height: "calc(100% - 40px)", overflowY: "scroll", padding: "8px 24px 24px" }}>
      <DialogBody>{content}</DialogBody>
    </div>
  );
}
