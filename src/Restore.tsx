import { toaster } from "@decky/api";
import { ConfirmModal, DialogBody, DialogButton, Focusable, Navigation, showModal } from "@decky/ui";
import { ReactNode, useEffect, useState } from "react";
import { Backup, cancelJob, clearJob, Drive, formatBytes, listBackups, listDrives, startRestore } from "./backend";
import { Card, Progress, Section, Text, useJob } from "./ui";

type Step = "backup" | "drive" | "restore" | "done";

export function Restore() {
  const [step, setStep] = useState<Step>("backup");
  const [backups, setBackups] = useState<Backup[]>([]);
  const [backup, setBackup] = useState<Backup | null>(null);
  const [drives, setDrives] = useState<Drive[]>([]);
  const [drive, setDrive] = useState<Drive | null>(null);
  const [message, setMessage] = useState("");
  const job = useJob();

  useEffect(() => {
    listBackups().then(setBackups);
  }, []);
  useEffect(() => {
    if (step === "drive") listDrives().then(setDrives);
  }, [step]);

  useEffect(() => {
    if (!job || job.type !== "restore") return;
    if (job.phase === "done" && step === "restore") {
      setMessage(job.message);
      clearJob();
      setStep("done");
    } else if (["error", "cancelled"].includes(job.phase)) {
      toaster.toast({ title: "Booty", body: job.message });
      clearJob();
      setStep("drive");
    }
  }, [job]);

  const fail = (e: any) => toaster.toast({ title: "Booty", body: String(e?.message ?? e) });

  const confirm = (d: Drive) => {
    showModal(
      <ConfirmModal
        strTitle={`Restore to ${d.model}?`}
        strDescription={
          `${d.model} (${formatBytes(d.size)}) will be erased, formatted as exFAT, and the ${backup!.files} files ` +
          `from "${backup!.name}" copied back onto it. The backup stays on the Deck.` +
          (d.has_games ? " This drive has Steam games on it, and they'll be erased." : "")
        }
        strOKButtonText="Erase and restore"
        bDestructiveWarning={true}
        onOK={() => {
          setDrive(d);
          startRestore(backup!.name, d.path, d.size).then(() => setStep("restore"), fail);
        }}
      />
    );
  };

  let content: ReactNode;
  if (step === "backup") {
    content = (
      <Section title="Restore a USB backup">
        <Text>Backups are kept in the "Booty Backups" folder in your home folder. Pick one to copy back onto a drive.</Text>
        {backups.map((b) => (
          <Card key={b.name} title={b.name}
            body={`${b.files} files, ${formatBytes(b.bytes)}. From ${b.model} (${formatBytes(b.size)}), saved ${b.created}.`}
            onClick={() => {
              setBackup(b);
              setStep("drive");
            }} />
        ))}
        {backups.length === 0 && <Text>There are no backups yet.</Text>}
        <DialogButton style={{ width: "160px" }} onClick={() => Navigation.NavigateBack()}>Back</DialogButton>
      </Section>
    );
  } else if (step === "drive") {
    content = (
      <Section title={`Copy "${backup?.name}" back to...`}>
        <Text>The Deck's own drive is never listed. The drive you pick is erased first.</Text>
        {drives.map((d) => (
          <Card key={d.path} title={`${d.model} (${formatBytes(d.size)})`}
            body={`${d.kind}  ${d.path}${d.labels.length ? "  " + d.labels.join(", ") : ""}`}
            warn={d.has_games ? "Has your Steam games on it" : undefined}
            onClick={() => confirm(d)} />
        ))}
        {drives.length === 0 && <Text>No USB drive found. Plug one in, then refresh.</Text>}
        <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
          <DialogButton onClick={() => listDrives().then(setDrives)}>Refresh</DialogButton>
          <DialogButton onClick={() => setStep("backup")}>Back</DialogButton>
        </Focusable>
      </Section>
    );
  } else if (step === "restore") {
    content = (
      <Section title={`Restoring to ${drive?.model ?? ""}`}>
        <Progress job={job} onCancel={() => cancelJob()} />
      </Section>
    );
  } else {
    content = (
      <Section title="Restored">
        <Text>{message} You can unplug it now. The backup is still on the Deck in "Booty Backups".</Text>
        <DialogButton style={{ width: "160px" }} onClick={() => Navigation.NavigateBack()}>Done</DialogButton>
      </Section>
    );
  }

  return (
    <div style={{ marginTop: "40px", height: "calc(100% - 40px)", overflowY: "scroll", padding: "8px 24px 24px" }}>
      <DialogBody>{content}</DialogBody>
    </div>
  );
}
