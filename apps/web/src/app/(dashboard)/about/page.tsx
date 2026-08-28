import Image from "next/image";
import styles from "./about.module.css";

export default function AboutPage() {
  return (
    <article className={styles.wrap}>
      <header className={styles.hero}>
        <div className={styles.heroCopy}>
          <h1>About Level</h1>
          <p>
            Level is a caregiver&apos;s second set of hands - built for the{" "}
            <a href="https://allthingsagentichackathon.devpost.com/" target="_blank" rel="noreferrer">
              All Things Agentic Hackathon
            </a>{" "}
            in the Collaborative Partner track.
          </p>
        </div>
        <div className={styles.art} aria-hidden="true">
          <div className={styles.artFrame}>
            <Image
              src="/about-care.jpg"
              alt=""
              width={1600}
              height={900}
              priority
              className={styles.people}
            />
          </div>
        </div>
      </header>

      <section>
        <h2>What Level does</h2>
        <ul>
          <li>Syncs your Google Calendar and never sends raw event bodies to any model.</li>
          <li>
            Uses Gemini 3.5 (Flash + Pro) via Vertex AI to infer the people you care for, your
            usual weekly rhythm, and the events you might be missing.
          </li>
          <li>
            Weighs your priorities when you ask &ldquo;find a time&rdquo; - and tells you when
            booking would collide with a Keep&apos;d usual.
          </li>
          <li>
            Drafts a short, courteous email to a teacher or doctor. You review, edit, and send.
          </li>
          <li>Speaks a two-sentence summary of your day when your hands are full.</li>
        </ul>
      </section>

      <section>
        <h2>What Level won&apos;t do</h2>
        <ul>
          <li>Send email or book calendar events without a human confirmation token.</li>
          <li>Send emails, phone numbers, or street addresses into any AI prompt.</li>
          <li>Follow instructions found inside your calendar summaries.</li>
        </ul>
      </section>

      <section>
        <h2>How Level learns</h2>
        <p>
          Tell Level in chat when an inference needs correction &mdash; &ldquo;Robert is my kid, not my dad,&rdquo;
          &ldquo;Alex is my co-parent,&rdquo; &ldquo;never miss time with my mom.&rdquo; Those
          corrections live on your profile, so the next pass doesn&apos;t invent the same thing.
          A missing usual you mark Resolved, or as a different week, stays quiet until next Monday.
        </p>
      </section>
    </article>
  );
}
