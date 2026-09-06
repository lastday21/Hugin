import type { SearchOutcomes } from "../types";

export function OutcomeSummary({ outcomes }: { outcomes: SearchOutcomes }) {
  const reasons = Object.entries(outcomes.confirmed_rejection_reasons);
  return (
    <section className="dashboard-card outcome-card" aria-labelledby="outcome-title">
      <div className="card-heading">
        <div>
          <h3 id="outcome-title">Результат поиска</h3>
          <p>По подтверждённым отправкам Hugin за всё время.</p>
        </div>
      </div>
      <dl className="outcome-totals">
        <div><dt>Записано подтверждений приглашения на собеседование</dt><dd>{outcomes.confirmed_interview_invitations}</dd></div>
        <div><dt>Откликов с записанной датой собеседования</dt><dd>{outcomes.scheduled_interviews}</dd></div>
        <div><dt>Приглашений по статусу hh.ru</dt><dd>{outcomes.invitations}</dd></div>
        <div><dt>Подтверждённых отправок Hugin</dt><dd>{outcomes.sent_by_hugin}</dd></div>
      </dl>
      <p className="outcome-explanation">
        Приглашение ещё не означает назначенную встречу. Молчание работодателя само по
        себе не доказывает нехватку опыта или ошибку программы. Если подтверждения ещё
        не записаны, ноль в счётчике не означает, что приглашений не было.
      </p>
      <details className="outcome-details">
        <summary>Результаты через 14 и 21 день и полнота сведений</summary>
        <div className="outcome-table-scroll">
          <table>
            <caption>Последние известные результаты. Отсчёт от подтверждения отправки; группа 21 дня входит в группу 14 дней. Строки могут пересекаться: после приглашения возможен отказ.</caption>
            <thead><tr><th scope="col">Показатель</th>{outcomes.cohorts.map((cohort) => (
              <th scope="col" key={cohort.age_days}>От {cohort.age_days} дней</th>
            ))}</tr></thead>
            <tbody>
              {([
                ["applications", "Отправлено"],
                ["invitations", "Было приглашение по статусу hh.ru"],
                ["confirmed_interview_invitations", "Подтверждено приглашение на собеседование"],
                ["scheduled_interviews", "Согласована дата встречи"],
                ["rejections", "Зафиксирован отказ"],
                ["without_decision", "Нет приглашения или отказа"],
                ["checked_after_window", "Статус проверен после срока наблюдения"],
                ["checked_last_48_hours", "Статус проверен за последние 48 часов"],
                ["with_selection_snapshot", "Сохранена оценка на момент отправки"],
              ] as const).map(([key, label]) => (
                <tr key={key}><th scope="row">{label}</th>{outcomes.cohorts.map((cohort) => (
                  <td key={cohort.age_days}>{cohort[key]}</td>
                ))}</tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>Импортированные отклики и отправки с неустановленным авторством: {outcomes.imported_or_unattributed}. В эти группы они не входят.</p>
        <p>{reasons.length ? "Объяснения отказов, записанные пользователем с источником:" : "Объяснения причин отказов ещё не записаны. По этим сведениям причину отсутствия собеседований установить нельзя."}</p>
        {reasons.length > 0 && <ul>{reasons.map(([reason, count]) => <li key={reason}>{reason}: {count}</li>)}</ul>}
        <ul className="outcome-limitations">{outcomes.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
        <h4>Результаты версий отбора</h4>
        <p>Доля подтверждённых приглашений именно на собеседование среди откликов возрастом от 14 дней. Незаполненное подтверждение означает неизвестный результат; ноль записей не доказывает отсутствие приглашений. Сначала проверяем полноту и сопоставимость групп.</p>
        {(outcomes.versions ?? []).some((group) => group.age_days === 14) ? (
          <div className="outcome-table-scroll">
            <table>
              <caption>Накопленные приглашения. Разница долей сама по себе не доказывает улучшение программы.</caption>
              <thead><tr><th scope="col">Версия</th><th scope="col">Приглашения / отклики</th><th scope="col">Доля</th><th scope="col">Возраст, дней</th><th scope="col">Сохранён контекст</th><th scope="col">Проверен статус после 14 дней</th></tr></thead>
              <tbody>{(outcomes.versions ?? []).filter((group) => group.age_days === 14).map((group) => (
                <tr key={group.rules_version ?? "unknown"}>
                  <th scope="row">{group.rules_version ?? "Не сохранена"}</th>
                  <td>{group.confirmed_interview_invitations} / {group.applications}</td>
                  <td>{group.confirmed_interview_invitations_per_100 === null ? "—" : `${group.confirmed_interview_invitations_per_100.toLocaleString("ru-RU")}%`}</td>
                  <td>{group.youngest_days}–{group.oldest_days}</td>
                  <td>{group.with_outcome_context} / {group.applications}</td>
                  <td>{group.checked_after_window} / {group.applications}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p>Зрелых откликов для сравнения версий пока нет.</p>}
        <ul>{(outcomes.comparison_limitations ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
      </details>
    </section>
  );
}
