import { definePlugin, routerHook } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { FaExchangeAlt } from "react-icons/fa";
import { refresh } from "./backend";
import { Panel } from "./Panel";
import { patchPowerMenu } from "./powerMenu";
import { Restore } from "./Restore";
import { Setup } from "./Setup";

export default definePlugin(() => {
  refresh().catch((e) => console.error("[Booty] initial load failed", e));
  const unpatchPowerMenu = patchPowerMenu();
  routerHook.addRoute("/booty", Setup, { exact: true });
  routerHook.addRoute("/booty-restore", Restore, { exact: true });

  return {
    name: "Booty",
    titleView: <div className={staticClasses.Title}>Booty</div>,
    content: <Panel />,
    icon: <FaExchangeAlt />,
    onDismount() {
      unpatchPowerMenu();
      routerHook.removeRoute("/booty");
      routerHook.removeRoute("/booty-restore");
    },
  };
});
