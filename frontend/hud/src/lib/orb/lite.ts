import { WAVE_POINTS, type OrbFrame } from './animator';

const TAU = Math.PI * 2;

const rgba = (c: readonly number[], a: number): string =>
  `rgba(${Math.round((c[0] ?? 0) * 255)},${Math.round((c[1] ?? 0) * 255)},${Math.round((c[2] ?? 0) * 255)},${Math.max(0, Math.min(1, a))})`;

/** Canvas2D fallback ("lite" tier): the same frame parameters as the shader, drawn with a handful of strokes. */
export class LiteOrb {
  private ctx: CanvasRenderingContext2D;

  private constructor(
    private canvas: HTMLCanvasElement,
    ctx: CanvasRenderingContext2D
  ) {
    this.ctx = ctx;
  }

  static create(canvas: HTMLCanvasElement): LiteOrb | null {
    try {
      const ctx = canvas.getContext('2d', { alpha: true });
      return ctx ? new LiteOrb(canvas, ctx) : null;
    } catch {
      return null;
    }
  }

  resize(width: number, height: number): void {
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
  }

  draw(f: OrbFrame): void {
    const { ctx, canvas } = this;
    const w = canvas.width;
    const h = canvas.height;
    const half = Math.min(w, h) / 2;
    const cx = w / 2;
    const cy = h / 2;
    const R = f.radius * half;
    ctx.clearRect(0, 0, w, h);
    ctx.globalAlpha = f.alpha;
    ctx.lineCap = 'round';
    const line = Math.max(1, half * 0.02);

    // Core glow.
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.9);
    g.addColorStop(0, rgba(f.color, Math.min(1, f.core * 1.2)));
    g.addColorStop(1, rgba(f.color, 0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.9, 0, TAU);
    ctx.fill();

    ctx.strokeStyle = rgba(f.color, 0.95);
    ctx.lineWidth = line;
    if (f.speaking > 0.05) {
      // Radial waveform: ring radius displaced by the output envelope history, mirrored for symmetry.
      ctx.beginPath();
      const n = 96;
      for (let i = 0; i <= n; i++) {
        const a = (i / n) * TAU;
        const u = i / n < 0.5 ? (i / n) * 2 : 2 - (i / n) * 2;
        const idx = Math.min(WAVE_POINTS - 1, Math.round(u * (WAVE_POINTS - 1)));
        const r = R + f.speaking * (f.wave[idx] ?? 0) * 0.2 * half;
        const x = cx + Math.cos(a) * r;
        const y = cy + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.stroke();
    } else if (f.segments > 0) {
      // Wake stages: four segments, done solid, active pulsing, remaining dim.
      const span = TAU / 4 - 0.3;
      for (let i = 0; i < f.segments; i++) {
        const start = i * (TAU / 4) + 0.04 + Math.PI;
        const level = i < f.segActive ? 0.95 : i === f.segActive ? 0.45 + 0.5 * f.segPulse : 0.16;
        ctx.strokeStyle = rgba(f.color, level);
        ctx.lineWidth = line * 2;
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.9, start, start + span);
        ctx.stroke();
      }
      if (f.sweep > 0) {
        ctx.strokeStyle = rgba(f.color, 0.85);
        ctx.lineWidth = line;
        ctx.beginPath();
        ctx.arc(cx, cy, R * 1.14, f.sweepAngle, f.sweepAngle + 1.2);
        ctx.stroke();
      }
    } else {
      // Main ring: dashed (segmented) with a gap when broken.
      const gap = f.broken > 0.5 ? 0.7 : 0;
      ctx.setLineDash([R * 0.18, R * 0.07]);
      ctx.lineDashOffset = -f.arcAngle * R;
      ctx.beginPath();
      ctx.arc(cx, cy, R, gap ? 0.9 + gap : 0, gap ? 0.9 + TAU : TAU);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.strokeStyle = rgba(f.color, 0.5);
      ctx.lineWidth = line * 0.7;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.06, f.arcAngle, f.arcAngle + 1.1);
      ctx.stroke();
      if (f.counter > 0) {
        ctx.strokeStyle = rgba(f.color, 0.9);
        ctx.lineWidth = line * 1.4;
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.84, f.counterAngle, f.counterAngle + 1.9);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(cx, cy, R * 1.1, -f.counterAngle * 0.8 + 3.2, -f.counterAngle * 0.8 + 4.7);
        ctx.stroke();
      }
    }

    if (f.flash > 0) {
      ctx.globalAlpha = f.flash * 0.6;
      ctx.fillStyle = 'rgb(255,77,94)';
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, TAU);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
}
