// p5.brush ships no types. `Brush` covers only the methods the garden calls.
export type Point = [number, number];

export interface Brush {
  createCanvas(
    w: number,
    h: number,
    opts?: { parent?: string | HTMLElement | null; pixelDensity?: number },
  ): unknown;
  clear(color?: string): void;
  render(): void;
  push(): void;
  pop(): void;
  translate(x: number, y: number): void;
  noStroke(): void;
  noFill(): void;
  noHatch(): void;
  noWash(): void;
  fill(color: string, alpha: number): void;
  fillBleed(strength: number, direction?: "out" | "in"): void;
  fillTexture(strength: number, border: number, scatter?: boolean): void;
  polygon(points: Point[]): unknown;
  wash(color: string, alpha: number): void;
  rect(x: number, y: number, w: number, h: number, mode?: "corner" | "center"): void;
}

export interface PetalShape {
  skew: number;
  tip: number;
  taper: number;
  ruffle: number;
  ruffleFreq: number;
}

export interface Pose {
  theta: number;
  base: number;
  len: number;
  aspect: number;
  tone: number;
  curl?: number;
  dx?: number;
  dy?: number;
  shadow?: boolean;
  twoTone?: boolean;
  // Set by createFlower(), so present on every pose that gets painted.
  shape?: PetalShape;
}

export type CoreType = "none" | "dot-dark" | "dot-yellow" | "dot-tiny" | "lotus" | "stamen" | "calyx";
export type LeafType = "none" | "oval" | "long" | "lobed" | "serrate" | "pad";

export interface PaletteSpec {
  hMin: number;
  hMax: number;
  sMin: number;
  sMax: number;
  lOuter: number;
  lInner: number;
  spread: number;
}

export type Palette = (tone: number) => string;

interface WhorlBase {
  base: number;
  len: number;
  aspect: number;
  tone: number;
  curl?: number;
  twoTone?: boolean;
}

export interface RingWhorl extends WhorlBase {
  kind?: "ring";
  count: number;
  phase?: number;
}

export interface FanWhorl extends WhorlBase {
  kind: "fan";
  count: number;
  center: number;
  span: number;
  cup?: number;
}

export interface ClusterRingWhorl {
  kind: "cluster";
  clusterKind?: "ring";
  count: number;
  floretPetals: number;
  base: number;
  len: number;
  aspect: number;
  tone: number;
  offsetMin: number;
  offsetMax: number;
  includeCenter?: boolean;
}

export interface ClusterDiscWhorl {
  kind: "cluster";
  clusterKind: "disc";
  count: number;
  floretPetals: number;
  base: number;
  len: number;
  aspect: number;
  // Ignored when toneRandom is set: each floret then picks its own tone.
  tone?: number;
  radiusX: number;
  radiusY: number;
  toneRandom?: boolean;
}

export type Whorl = RingWhorl | FanWhorl | ClusterRingWhorl | ClusterDiscWhorl;

export interface FlowerSpec {
  size?: number;
  shape: PetalShape | PetalShape[];
  palette: PaletteSpec;
  whorls: Whorl[];
  core?: CoreType;
  leaf: LeafType;
}

export interface Flower {
  pal: Palette;
  core: CoreType;
  size: number;
  poses: Pose[];
}
