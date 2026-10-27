import { findModuleDetailsByExport, MenuItem, MenuSeparator } from "@decky/ui";
import { Children, cloneElement, ReactElement } from "react";
import { getState } from "./backend";
import { confirmSwitch } from "./confirm";

// Steam doesn't expose its power menu, so we catch it when its context menu is
// created (it's the only menu opened with an `isPowerChord` prop), wrap the
// menu component's render once, and append our "Switch to ..." items.
export function patchPowerMenu(): () => void {
  const [, mgrModule] = findModuleDetailsByExport(
    (e: any) => typeof e?.GetContextMenuManagerFromWindow === "function"
  );
  if (!mgrModule) {
    console.error("[Booty] context menu manager not found; power menu items disabled");
    return () => {};
  }
  const proto = Object.getPrototypeOf(mgrModule.GetContextMenuManagerFromWindow(window));
  const originalCreate = proto.CreateContextMenuInstance;
  const wrapped = new Map<any, Function>();

  const addItems = (menu: ReactElement<any>) => {
    const { entries, settings } = getState();
    const targets = entries.filter((e) => e.switchable);
    if (!settings?.show_in_power_menu || targets.length === 0 || !menu?.props) return menu;
    const children = Children.toArray(menu.props.children);
    children.push(<MenuSeparator key="booty-sep" />);
    for (const t of targets) {
      children.push(
        <MenuItem key={`booty-${t.id}`} onSelected={() => confirmSwitch(t)}>
          {`Switch to ${t.name}`}
        </MenuItem>
      );
    }
    return cloneElement(menu, {}, ...children);
  };

  proto.CreateContextMenuInstance = function (element: any, ...rest: any[]) {
    try {
      const memo = element?.type;
      if (element?.props && "isPowerChord" in element.props && memo?.type && !wrapped.has(memo)) {
        const inner = memo.type;
        memo.type = function (this: unknown, props: unknown) {
          return addItems(inner.call(this, props));
        };
        wrapped.set(memo, inner);
      }
    } catch (e) {
      console.error("[Booty] power menu patch failed", e);
    }
    return originalCreate.call(this, element, ...rest);
  };

  return () => {
    proto.CreateContextMenuInstance = originalCreate;
    wrapped.forEach((inner, memo) => {
      memo.type = inner;
    });
    wrapped.clear();
  };
}
