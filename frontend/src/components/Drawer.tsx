import { useEffect, type ReactNode } from "react";

/** 右侧滑出抽屉（Esc 关闭）。 */
export function Drawer({
  title,
  onClose,
  children,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div className="drawer-mask" onClick={onClose} aria-hidden />
      <aside className="drawer" role="dialog" aria-label={typeof title === "string" ? title : "详情"}>
        <div className="drawer-head">
          <h2>{title}</h2>
          <button className="btn-ghost btn-sm" onClick={onClose} aria-label="关闭">
            关闭 ✕
          </button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}
