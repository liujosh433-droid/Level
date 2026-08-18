import styles from "./about.module.css";

export default function AboutPage() {
  return (
    <article className={styles.wrap}>
      <header>
        <h1>About Level</h1>
        <p>
          Level is a caregiver&apos;s second set of hands - built for the{" "}
          <a href="https://allthingsagentichackathon.devpost.com/" target="_blank" rel="noreferrer">
            All Things Agentic Hackathon
          </a>{" "}
          in the Collaborative Partner track.
        </p>
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
        <h2>Feedback loop</h2>
        <p>
          Every &ldquo;Not me&rdquo; you tap is stored in a <code>negatives</code> collection and
          injected into the next agent call as &ldquo;do not propose this again.&rdquo; Level
          adapts to your particular family shape without a training run.
        </p>
      </section>
    </article>
  );
}
