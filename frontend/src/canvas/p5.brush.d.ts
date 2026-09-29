// p5.brush ships no types. garden.ts casts the module to `Brush` in
// types.ts; this only lets the import resolve.
declare module "p5.brush/standalone" {
  const brush: unknown;
  export default brush;
}
