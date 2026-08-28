import WeeklyRecap from "@/components/WeeklyRecap";
import styles from "./week.module.css";

/**
 * Week view. Currently hosts the Veo-generated weekly recap tile;
 * this is where we'll add other week-scale surfaces over time
 * (held-load history, missing-usuals trend, etc.) so /today can
 * stay focused on the next 48 hours and /week owns the "step back
 * and look at the whole week" story.
 *
 * The recap doesn't belong on /about (marketing surface) or /today
 * (48-hour operational view) - it's a reflective, weekly-cadence
 * artifact that deserves its own page.
 */
export default function WeekPage() {
  return (
    <article className={styles.wrap}>
      <header className={styles.hero}>
        <h1>Your week</h1>
        <p className={styles.lede}>
          A step-back view of what Level noticed this week. Auto-refreshes each
          Monday; you can regenerate any time if the week takes a sharp turn.
        </p>
      </header>
      <WeeklyRecap />
    </article>
  );
}
