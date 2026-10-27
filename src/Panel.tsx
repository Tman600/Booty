import {
  ButtonItem,
  ConfirmModal,
  Navigation,
  PanelSection,
  PanelSectionRow,
  showModal,
  ToggleField,
} from "@decky/ui";
import { useState } from "react";
import { addReturnButtons, refresh, repairEntries, updateSetting, useBootyState } from "./backend";
import { confirmSwitch } from "./confirm";

export function Panel() {
  const { entries, settings } = useBootyState();
  const targets = entries.filter((e) => e.switchable);
  const [adding, setAdding] = useState(false);
  const [finding, setFinding] = useState(false);

  const findOses = async () => {
    setFinding(true);
    let body: string;
    try {
      const res = await repairEntries();
      await refresh();
      body = res.found.length
        ? res.found
            .map((f) => `${f.name} on ${f.partition}${res.created.includes(f.name) ? " (boot entry restored)" : ""}`)
            .join(". ") + (res.tidied.length ? `. Cleaned up the boot menu in ${res.tidied.join(", ")}.` : "")
        : "No installed OS found on the SD card or other drives.";
    } catch (e: any) {
      body = `Something went wrong: ${e?.message ?? e}`;
    }
    setFinding(false);
    showModal(<ConfirmModal strTitle="Installed OSes" strDescription={body} bAlertDialog={true} />);
  };

  const addButtons = async () => {
    setAdding(true);
    let body: string;
    try {
      const results = await addReturnButtons();
      body = results.length
        ? results.map((r) => `${r.ok ? "Done" : "Not added"}: ${r.name} (${r.partition}). ${r.detail}`).join("\n\n")
        : "Booty didn't find another installed OS. Install one first, then come back here.";
    } catch (e: any) {
      body = `Something went wrong: ${e?.message ?? e}`;
    }
    setAdding(false);
    showModal(<ConfirmModal strTitle="Back to SteamOS" strDescription={body} bAlertDialog={true} />);
  };

  return (
    <>
      <PanelSection title="Switch OS">
        {targets.map((t) => (
          <PanelSectionRow key={t.id}>
            <ButtonItem layout="below" onClick={() => confirmSwitch(t)}>
              {`Switch to ${t.name}`}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {targets.length === 0 && (
          <PanelSectionRow>
            <div style={{ fontSize: "13px", opacity: 0.8 }}>
              No other operating system found yet. Set one up below, or find one that's already installed.
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={finding} onClick={findOses}
            description="Looks on the SD card and other drives for installed OSes and restores their boot entries.">
            {finding ? "Looking..." : "Find installed OSes"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Set up">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              Navigation.Navigate("/booty");
              Navigation.CloseSideMenus();
            }}
          >
            Set up another OS
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="USB backups">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="Copy files saved from a USB stick back onto a drive."
            onClick={() => {
              Navigation.Navigate("/booty-restore");
              Navigation.CloseSideMenus();
            }}
          >
            Restore a USB backup
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Back to SteamOS">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={adding}
            description="Puts a Back to SteamOS button in your other OSes: on the Windows desktop, and in the Linux app menu."
            onClick={addButtons}
          >
            {adding ? "Adding..." : "Add Back to SteamOS buttons"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {settings && (
        <PanelSection title="Options">
          <PanelSectionRow>
            <ToggleField
              label="Show in power menu"
              checked={settings.show_in_power_menu}
              onChange={(v) => updateSetting("show_in_power_menu", v)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Keep SteamOS as default"
              description="Turning the Deck on always starts SteamOS, even after another OS's installer puts itself first."
              checked={settings.keep_steamos_default}
              onChange={(v) => updateSetting("keep_steamos_default", v)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}
