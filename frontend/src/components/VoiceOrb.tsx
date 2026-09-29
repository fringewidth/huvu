export function VoiceOrb() {
  return (
    <div className="relative z-10 flex h-screen flex-col items-center justify-center gap-4 [font-family:var(--serif)] text-foreground">
      <div className="size-30 rounded-full bg-[radial-gradient(circle,var(--accent),var(--bg))]" />
      <p>voice mode placeholder, wire in STT/TTS here</p>
    </div>
  );
}
