// Small helpers shared by the garden's drawing code.
export const TAU = Math.PI * 2;

export const rand = (a: number, b: number) => a + Math.random() * (b - a);
export const randInt = (a: number, b: number) => Math.floor(rand(a, b + 1));
export function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function hsl(h: number, s: number, l: number): string {
  h = ((h % 360) + 360) % 360;
  return `hsl(${h.toFixed(1)}, ${(Math.min(1, Math.max(0, s)) * 100).toFixed(1)}%, ${(Math.min(0.97, Math.max(0.3, l)) * 100).toFixed(1)}%)`;
}

// The same random numbers on every run, for the bench (bench.ts), on the
// page and in the garden's worker.
export function seedRandom(): void {
  let seed = 1;
  Math.random = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
}
