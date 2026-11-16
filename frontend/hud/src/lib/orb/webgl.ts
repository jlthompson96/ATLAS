import { WAVE_POINTS, type OrbFrame } from './animator';

const VERT = `#version 300 es
in vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

/**
 * One full-screen quad, everything drawn analytically in the fragment shader (PLAN.md §2.14).
 * Polar coordinates: rings, tick marks, arcs, wake segments, a radial waveform and a few particles.
 */
const FRAG = `#version 300 es
precision mediump float;
#define TAU 6.2831853
#define WAVE ${WAVE_POINTS}
uniform vec2 uRes;
uniform float uTime;
uniform vec3 uColor;
uniform float uCore;
uniform float uRadius;
uniform float uAlpha;
uniform float uArc;
uniform float uCounterAngle;
uniform float uSweepAngle;
uniform float uSegments;
uniform float uSegActive;
uniform float uSegPulse;
uniform float uBroken;
uniform float uFlash;
uniform float uLevel;
uniform float uParticles;
uniform float uCounter;
uniform float uSweep;
uniform float uSpeaking;
uniform float uFx;
uniform float uWave[WAVE];
out vec4 outColor;

float band(float d, float r, float w) { return 1.0 - smoothstep(0.0, w, abs(d - r)); }
// 1 inside an arc starting at 'start' of angular length 'len', soft edges.
float arc(float a, float start, float len) {
  float x = mod(a - start, TAU);
  float e = 0.05;
  return smoothstep(0.0, e, x) * (1.0 - smoothstep(len - e, len, x));
}

void main() {
  vec2 p = (gl_FragCoord.xy - 0.5 * uRes) / (0.5 * min(uRes.x, uRes.y));
  float d = length(p);
  float a = atan(p.y, p.x) + 3.14159265;
  float R = uRadius;

  // Radial waveform displacement (speaking). Mirror around the circle so it is seamless.
  float u = (a / TAU) * 2.0;
  u = u > 1.0 ? 2.0 - u : u;
  float fi = u * float(WAVE - 1);
  int i0 = int(floor(fi));
  int i1 = min(i0 + 1, WAVE - 1);
  float w = mix(uWave[i0], uWave[i1], fract(fi));
  float rr = R + uSpeaking * w * 0.2;

  float v = 0.0;

  // Main ring: segmented dashes drifting with arc angle; solid while speaking.
  float dash = mix(smoothstep(-0.3, 0.1, sin((a - uArc) * 24.0)), 1.0, uSpeaking);
  float gapMask = 1.0 - uBroken * arc(a, 0.9, 0.7);
  float ring = band(d, rr, 0.012) * mix(1.0, dash, 0.55) * gapMask;
  v += ring * 0.95;
  v += band(d, rr, 0.05) * 0.18 * gapMask;

  // Tick marks outside the ring.
  float ticks = step(fract(a / TAU * 72.0), 0.12) * band(d, R * 1.13, 0.01);
  v += ticks * 0.35 * gapMask;

  // Slowly rotating accent arc (idle/listening drift).
  v += band(d, R * 1.05, 0.007) * arc(a, uArc, 1.1) * 0.55;

  // Thinking: counter-rotating arcs.
  v += uCounter * band(d, R * 0.84, 0.014) * arc(a, uCounterAngle, 1.9) * 0.9;
  v += uCounter * band(d, R * 1.08, 0.012) * arc(a, -uCounterAngle * 0.8 + 3.2, 1.5) * 0.8;

  // Wake segments: four 78 degree segments. Done ones solid, active one pulsing, rest dim outline.
  if (uSegments > 0.5) {
    float seg = floor(a / (TAU / 4.0));
    float local = mod(a, TAU / 4.0);
    float inSeg = smoothstep(0.0, 0.04, local) * (1.0 - smoothstep(TAU / 4.0 - 0.3, TAU / 4.0 - 0.26, local));
    float done = step(seg + 0.5, uSegActive);
    float act = 1.0 - step(0.5, abs(seg - uSegActive));
    float lvl = done * 0.95 + act * (0.45 + 0.5 * uSegPulse) + (1.0 - done) * (1.0 - act) * 0.16;
    v += band(d, R * 0.9, 0.022) * inSeg * lvl;
    v += uSweep * band(d, R * 1.1, 0.01) * arc(a, uSweepAngle, 1.2) * 0.85;
  }

  // Orbiting particles (thinking).
  if (uParticles > 0.5) {
    for (int k = 0; k < 10; k++) {
      float fk = float(k);
      float ang = uTime * (0.5 + 0.17 * fk) * (mod(fk, 2.0) < 1.0 ? 1.0 : -1.0) + fk * 0.628;
      float pr = R * (0.7 + 0.05 * fk);
      vec2 pp = vec2(cos(ang), sin(ang)) * pr;
      v += (1.0 - smoothstep(0.0, 0.022, length(p - pp))) * 0.9;
    }
  }

  // Core glow; it also swells with the audio level.
  float core = exp(-d * d * 9.0) * uCore + exp(-d * d * 60.0) * uCore * 0.6;
  v += core;

  vec3 col = uColor * v;
  // Error flash: red wash from the centre.
  col += vec3(1.0, 0.3, 0.36) * uFlash * exp(-d * d * 2.5) * 0.8;
  // Soft vignette so the quad never shows a hard edge.
  float fade = 1.0 - smoothstep(0.92, 1.0, d);
  // Scanlines (full tier only).
  float scan = 1.0 - uFx * 0.06 * (0.5 + 0.5 * sin(gl_FragCoord.y * 3.14159));
  float alpha = clamp(max(max(col.r, col.g), col.b), 0.0, 1.0) * uAlpha * fade;
  outColor = vec4(col * scan * uAlpha * fade, alpha);
}
`;

type Uniforms = Record<string, WebGLUniformLocation | null>;

export class WebglOrb {
  private gl: WebGL2RenderingContext;
  private program: WebGLProgram;
  private u: Uniforms = {};
  private lost = false;
  private waveBuf = new Float32Array(WAVE_POINTS);
  onLost: (() => void) | null = null;

  private constructor(
    private canvas: HTMLCanvasElement,
    gl: WebGL2RenderingContext,
    program: WebGLProgram
  ) {
    this.gl = gl;
    this.program = program;
    for (const name of [
      'uRes', 'uTime', 'uColor', 'uCore', 'uRadius', 'uAlpha', 'uArc', 'uCounterAngle', 'uSweepAngle', 'uSegments',
      'uSegActive', 'uSegPulse', 'uBroken', 'uFlash', 'uLevel', 'uParticles', 'uCounter', 'uSweep', 'uSpeaking', 'uFx', 'uWave'
    ]) {
      this.u[name] = gl.getUniformLocation(program, name);
    }
    canvas.addEventListener('webglcontextlost', (e) => {
      e.preventDefault();
      this.lost = true;
      this.onLost?.();
    });
  }

  /** Returns null when WebGL2 or the shader is unavailable; the caller falls back to the lite renderer. */
  static create(canvas: HTMLCanvasElement): WebglOrb | null {
    let gl: WebGL2RenderingContext | null = null;
    try {
      gl = canvas.getContext('webgl2', { alpha: true, antialias: false, depth: false, stencil: false, powerPreference: 'low-power' });
    } catch {
      return null;
    }
    if (!gl) return null;
    const compile = (type: number, src: string): WebGLShader | null => {
      const sh = gl.createShader(type);
      if (!sh) return null;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.warn('orb shader failed to compile', gl.getShaderInfoLog(sh));
        return null;
      }
      return sh;
    };
    const vs = compile(gl.VERTEX_SHADER, VERT);
    const fs = compile(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return null;
    const program = gl.createProgram();
    if (!program) return null;
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.warn('orb program failed to link', gl.getProgramInfoLog(program));
      return null;
    }
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(program, 'aPos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.useProgram(program);
    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    return new WebglOrb(canvas, gl, program);
  }

  resize(width: number, height: number): void {
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.gl.viewport(0, 0, width, height);
  }

  draw(f: OrbFrame): void {
    if (this.lost) return;
    const { gl, u } = this;
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform2f(u.uRes ?? null, this.canvas.width, this.canvas.height);
    gl.uniform1f(u.uTime ?? null, f.time);
    gl.uniform3f(u.uColor ?? null, f.color[0], f.color[1], f.color[2]);
    gl.uniform1f(u.uCore ?? null, f.core);
    gl.uniform1f(u.uRadius ?? null, f.radius);
    gl.uniform1f(u.uAlpha ?? null, f.alpha);
    gl.uniform1f(u.uArc ?? null, f.arcAngle);
    gl.uniform1f(u.uCounterAngle ?? null, f.counterAngle);
    gl.uniform1f(u.uSweepAngle ?? null, f.sweepAngle);
    gl.uniform1f(u.uSegments ?? null, f.segments);
    gl.uniform1f(u.uSegActive ?? null, f.segActive);
    gl.uniform1f(u.uSegPulse ?? null, f.segPulse);
    gl.uniform1f(u.uBroken ?? null, f.broken);
    gl.uniform1f(u.uFlash ?? null, f.flash);
    gl.uniform1f(u.uLevel ?? null, f.level);
    gl.uniform1f(u.uParticles ?? null, f.particles);
    gl.uniform1f(u.uCounter ?? null, f.counter);
    gl.uniform1f(u.uSweep ?? null, f.sweep);
    gl.uniform1f(u.uSpeaking ?? null, f.speaking);
    gl.uniform1f(u.uFx ?? null, f.fx);
    for (let i = 0; i < WAVE_POINTS; i++) this.waveBuf[i] = f.wave[i] ?? 0;
    gl.uniform1fv(u.uWave ?? null, this.waveBuf);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  dispose(): void {
    // Keep the context alive: the effect may re-create a renderer on the same canvas, and a lost
    // context can't be revived. Discarding the <canvas> element (keyed by tier) releases it.
    if (!this.lost) this.gl.deleteProgram(this.program);
  }
}
