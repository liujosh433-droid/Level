"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import styles from "./AboutIntro.module.css";

const POSTER = "/about-care.jpg";
const POLL_MS = 6000;

interface IntroResponse {
  ready: boolean;
  video_url?: string | null;
  generating?: boolean;
  model?: string | null;
}

function veoCredit(model: string | null | undefined): string {
  const raw = (model ?? "").toLowerCase();
  const ver = raw.match(/veo-(\d+\.\d+)/);
  const version = ver ? ver[1] : "3.1";
  if (raw.includes("fast")) return `Veo ${version} Fast`;
  return `Veo ${version}`;
}

/**
 * One-shot Veo film on Info. First visitor may wait while Veo runs;
 * after that every load is just the cached URL. The still photo stays
 * up if media is off or Veo is unavailable so the page never looks broken.
 */
export default function AboutIntro() {
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function load() {
      try {
        const data = await api.get<IntroResponse>("/v1/media/intro");
        if (cancelled) return;
        if (data.ready && data.video_url) {
          setVideoUrl(data.video_url);
          setModel(data.model ?? null);
          setGenerating(false);
          return;
        }
        if (data.generating) {
          setGenerating(true);
          timer = setTimeout(load, POLL_MS);
          return;
        }
        setGenerating(false);
      } catch {
        if (!cancelled) setGenerating(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return (
    <div className={styles.art}>
      <div className={styles.frame}>
        {videoUrl ? (
          <video
            className={styles.film}
            src={videoUrl}
            poster={POSTER}
            autoPlay
            muted
            loop
            playsInline
            controls
            aria-label="Short film about Level"
          />
        ) : (
          <Image
            src={POSTER}
            alt=""
            width={1600}
            height={900}
            priority
            className={styles.film}
          />
        )}
      </div>
      {videoUrl ? (
        <p className={styles.credit}>Generated with {veoCredit(model)}</p>
      ) : generating ? (
        <p className={styles.note}>Cooking a short Level film. This only happens once.</p>
      ) : null}
    </div>
  );
}
