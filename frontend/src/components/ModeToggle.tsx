import type { Mode } from "../config/interaction";
import { Button } from "./ui/button";

interface ModeToggleProps {
  mode: Mode;
  onChange: (mode: Mode) => void;
}

export function ModeToggle({ mode, onChange }: ModeToggleProps) {
  return (
    <Button
      variant="outline"
      className="fixed top-4 right-4 z-10 rounded-full border-border bg-card/40 text-foreground backdrop-blur-md hover:bg-card/70"
      onClick={() => onChange(mode === "text" ? "voice" : "text")}
    >
      switch to {mode === "text" ? "voice" : "text"}
    </Button>
  );
}
