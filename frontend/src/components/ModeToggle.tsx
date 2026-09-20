import type { Mode } from "../config/interaction";
import "../styles/ModeToggle.css";

interface ModeToggleProps {
  mode: Mode;
  onChange: (mode: Mode) => void;
}

export function ModeToggle({ mode, onChange }: ModeToggleProps) {
  return (
    <button
      type="button"
      className="mode-toggle"
      onClick={() => onChange(mode === "text" ? "voice" : "text")}
    >
      switch to {mode === "text" ? "voice" : "text"}
    </button>
  );
}
