// p5.brush ships no types. GardenCanvas.tsx imports the module namespace
// and casts it to the `Brush` interface in types.ts, which is the actual
// source of truth for what we call on it — this just satisfies the
// import itself.
declare module "p5.brush/standalone" {
  const brush: unknown;
  export default brush;
}
