import { ConfirmModal, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { BootEntry, switchTo } from "./backend";

export function confirmSwitch(entry: BootEntry) {
  showModal(
    <ConfirmModal
      strTitle={`Switch to ${entry.name}?`}
      strDescription={`The Deck will restart into ${entry.name}. Restarting from there brings you back to SteamOS.`}
      strOKButtonText="Restart"
      onOK={() =>
        switchTo(entry.id).catch((e) =>
          toaster.toast({ title: "Booty", body: `Couldn't switch: ${e?.message ?? e}` })
        )
      }
    />
  );
}
