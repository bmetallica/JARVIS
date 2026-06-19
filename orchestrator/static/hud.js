"use strict";

/*
 * JARVIS HUD — Canvas-Nachbau der Vorlagen-Animation (PyQt -> Web).
 * Zustände: IDLE · LISTENING · THINKING · SPEAKING · MUTED
 * Energie (Skalierung/Halo/Pulse/Partikel) wird pro Zustand sanft angefahren.
 */

const HUD_COLORS = {
    bg:    "#00060a",
    pri:   [0, 212, 255],   // Cyan  – IDLE
    green: [0, 255, 136],   // LISTENING
    acc2:  [255, 204, 0],   // Gelb  – THINKING
    acc:   [255, 107, 0],   // Orange– SPEAKING
    muted: [255, 51, 102],  // MUTED
};

// Zustand → {Farbe, Zielenergie 0..1, Label}
const HUD_STATES = {
    IDLE:      { col: HUD_COLORS.pri,   energy: 0.18, label: "BEREIT" },
    LISTENING: { col: HUD_COLORS.green, energy: 0.45, label: "HÖRE ZU" },
    THINKING:  { col: HUD_COLORS.acc2,  energy: 0.55, label: "DENKE NACH" },
    SPEAKING:  { col: HUD_COLORS.acc,   energy: 1.0,  label: "SPRECHE" },
    MUTED:     { col: HUD_COLORS.muted, energy: 0.05, label: "STUMM" },
};

class JarvisHUD {
    constructor(canvas) {
        this.cv = canvas;
        this.ctx = canvas.getContext("2d");
        this.state = "IDLE";
        this.col = HUD_COLORS.pri.slice();
        this.tgtCol = HUD_COLORS.pri.slice();
        this.energy = 0.18;
        this.tgtEnergy = 0.18;
        this.tick = 0;
        this.rings = [0, 120, 240];
        this.scan = 0; this.scan2 = 180;
        this.pulses = [0, 60, 120];
        this.particles = [];
        this.blink = true; this.blinkTick = 0;
        this.bars = new Array(32).fill(0);
        this._resize();
        window.addEventListener("resize", () => this._resize());
        requestAnimationFrame(() => this._loop());
    }

    setState(s) {
        if (!HUD_STATES[s]) return;
        this.state = s;
        const def = HUD_STATES[s];
        this.tgtCol = def.col.slice();
        this.tgtEnergy = def.energy;
    }

    _resize() {
        const dpr = window.devicePixelRatio || 1;
        const r = this.cv.getBoundingClientRect();
        this.cv.width = Math.max(1, Math.floor(r.width * dpr));
        this.cv.height = Math.max(1, Math.floor(r.height * dpr));
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this.W = r.width; this.H = r.height;
        this.fw = Math.min(this.W, this.H);
    }

    _rgba(c, a) { return `rgba(${c[0]|0},${c[1]|0},${c[2]|0},${a})`; }

    _loop() {
        this._step();
        this._draw();
        requestAnimationFrame(() => this._loop());
    }

    _step() {
        this.tick++;
        const speaking = this.state === "SPEAKING";
        // sanftes Annähern an Zielenergie/-farbe
        this.energy += (this.tgtEnergy - this.energy) * 0.08;
        for (let i = 0; i < 3; i++) this.col[i] += (this.tgtCol[i] - this.col[i]) * 0.1;

        const e = this.energy;
        const speeds = [0.55 + e * 1.1, -(0.35 + e * 0.7), 0.9 + e * 1.4];
        for (let i = 0; i < 3; i++) this.rings[i] = (this.rings[i] + speeds[i] + 360) % 360;
        this.scan = (this.scan + 1.3 + e * 2.2) % 360;
        this.scan2 = (this.scan2 - 0.75 - e * 1.5 + 360) % 360;

        // Pulsringe
        const lim = this.fw * 0.74;
        const psp = 1.6 + e * 3.0;
        this.pulses = this.pulses.map(r => r + psp).filter(r => r < lim);
        if (this.pulses.length < 3 && Math.random() < (0.02 + e * 0.06)) this.pulses.push(0);

        // Partikel beim Sprechen
        if (speaking && Math.random() < 0.30) {
            const ang = Math.random() * Math.PI * 2;
            const rs = this.fw * 0.20;
            this.particles.push({
                x: this.W / 2 + Math.cos(ang) * rs, y: this.H / 2 + Math.sin(ang) * rs,
                vx: Math.cos(ang) * (0.9 + Math.random() * 1.5),
                vy: Math.sin(ang) * (0.9 + Math.random() * 1.5) - 0.4, life: 1.0,
            });
        }
        this.particles = this.particles
            .map(p => ({ x: p.x + p.vx, y: p.y + p.vy, vx: p.vx * 0.97, vy: p.vy * 0.97, life: p.life - 0.028 }))
            .filter(p => p.life > 0);

        // Waveform-Balken
        for (let i = 0; i < this.bars.length; i++) {
            const target = e * (0.3 + 0.7 * Math.abs(Math.sin(this.tick * 0.15 + i * 0.5) + (Math.random() - 0.5) * e));
            this.bars[i] += (target - this.bars[i]) * 0.25;
        }

        if (++this.blinkTick >= 28) { this.blink = !this.blink; this.blinkTick = 0; }
    }

    _draw() {
        const ctx = this.ctx, W = this.W, H = this.H, cx = W / 2, cy = H / 2, fw = this.fw;
        const c = this.col, e = this.energy;
        ctx.clearRect(0, 0, W, H);

        // Halo
        const halo = 0.4 + e * 1.6;
        for (let i = 0; i < 10; i++) {
            const r = fw * 0.31 * (1.6 - i * 0.08);
            const a = Math.max(0, 0.10 * halo * (1 - i / 10));
            ctx.strokeStyle = this._rgba(c, a); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
        }

        // Pulsringe
        for (const pr of this.pulses) {
            const a = Math.max(0, 0.8 * (1 - pr / (fw * 0.74)));
            ctx.strokeStyle = this._rgba(c, a); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.arc(cx, cy, pr, 0, Math.PI * 2); ctx.stroke();
        }

        // 3 rotierende Bogenringe (gestrichelte Bögen)
        const ringDefs = [[0.42, 3, 115, 78], [0.35, 2, 78, 55], [0.28, 1, 56, 40]];
        ringDefs.forEach((d, idx) => {
            const [rf, w, arcL, gap] = d;
            const rr = fw * rf, base = this.rings[idx];
            ctx.strokeStyle = this._rgba(c, Math.min(1, 0.35 + e * 0.6) * (1 - idx * 0.18));
            ctx.lineWidth = w;
            let ang = base;
            while (ang < base + 360) {
                ctx.beginPath();
                ctx.arc(cx, cy, rr, ang * Math.PI / 180, (ang + arcL) * Math.PI / 180);
                ctx.stroke();
                ang += arcL + gap;
            }
        });

        // Scanner-Bögen
        const sr = fw * 0.46, ex = (40 + e * 35) * Math.PI / 180;
        ctx.strokeStyle = this._rgba(c, 0.6); ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(cx, cy, sr, this.scan * Math.PI / 180, this.scan * Math.PI / 180 + ex); ctx.stroke();
        ctx.strokeStyle = this._rgba(HUD_COLORS.acc, 0.4); ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(cx, cy, sr, this.scan2 * Math.PI / 180, this.scan2 * Math.PI / 180 + ex); ctx.stroke();

        // Tick-Marken
        ctx.strokeStyle = this._rgba(c, 0.5); ctx.lineWidth = 1;
        const tOut = fw * 0.49;
        for (let deg = 0; deg < 360; deg += 10) {
            const rad = deg * Math.PI / 180;
            const inn = (deg % 30 === 0) ? fw * 0.45 : fw * 0.47;
            ctx.beginPath();
            ctx.moveTo(cx + tOut * Math.cos(rad), cy - tOut * Math.sin(rad));
            ctx.lineTo(cx + inn * Math.cos(rad), cy - inn * Math.sin(rad));
            ctx.stroke();
        }

        // Fadenkreuz
        const chR = fw * 0.50, gapH = fw * 0.16;
        ctx.strokeStyle = this._rgba(c, 0.25 + e * 0.3); ctx.lineWidth = 1;
        [[-1, 0], [1, 0], [0, -1], [0, 1]].forEach(([dx, dy]) => {
            ctx.beginPath();
            ctx.moveTo(cx + dx * gapH, cy + dy * gapH);
            ctx.lineTo(cx + dx * chR, cy + dy * chR);
            ctx.stroke();
        });

        // Eck-Klammern
        const bl = 22, half = fw / 2;
        ctx.strokeStyle = this._rgba(c, 0.8); ctx.lineWidth = 2;
        [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([sx, sy]) => {
            const bx = cx + sx * half, by = cy + sy * half;
            ctx.beginPath();
            ctx.moveTo(bx, by); ctx.lineTo(bx - sx * bl, by);
            ctx.moveTo(bx, by); ctx.lineTo(bx, by - sy * bl);
            ctx.stroke();
        });

        // Orb (radialer Verlauf)
        const scale = 1 + (this.state === "SPEAKING" ? 0.06 + 0.06 * Math.abs(Math.sin(this.tick * 0.25)) : e * 0.03);
        const orbR = fw * 0.24 * scale;
        const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbR);
        grad.addColorStop(0, this._rgba(c, 0.55 + e * 0.35));
        grad.addColorStop(0.6, this._rgba(c, 0.12));
        grad.addColorStop(1, this._rgba(c, 0));
        ctx.fillStyle = grad;
        ctx.beginPath(); ctx.arc(cx, cy, orbR, 0, Math.PI * 2); ctx.fill();

        // Orb-Rand + Titel
        ctx.strokeStyle = this._rgba(c, 0.6 + e * 0.4); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(cx, cy, orbR, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = this._rgba(c, 0.9);
        ctx.font = `bold ${Math.max(11, fw * 0.05)}px "Courier New", monospace`;
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("J.A.R.V.I.S", cx, cy);

        // Partikel
        for (const p of this.particles) {
            ctx.fillStyle = this._rgba(c, Math.max(0, p.life));
            ctx.beginPath(); ctx.arc(p.x, p.y, 2.2, 0, Math.PI * 2); ctx.fill();
        }

        // Statustext
        const def = HUD_STATES[this.state] || HUD_STATES.IDLE;
        const sym = this.blink ? "●" : "○";
        ctx.fillStyle = this._rgba(c, 0.95);
        ctx.font = 'bold 12px "Courier New", monospace';
        ctx.fillText(`${sym}  ${def.label}`, cx, cy + fw * 0.40);

        // Waveform
        const N = this.bars.length, bw = 5, gap = 2, totalW = N * (bw + gap);
        const wx0 = cx - totalW / 2, wy = cy + fw * 0.40 + 24, maxH = fw * 0.10;
        for (let i = 0; i < N; i++) {
            const h = Math.max(2, this.bars[i] * maxH);
            const a = 0.3 + 0.6 * this.bars[i];
            ctx.fillStyle = this._rgba(c, a);
            ctx.fillRect(wx0 + i * (bw + gap), wy - h / 2, bw, h);
        }
    }
}

window.JarvisHUD = JarvisHUD;
