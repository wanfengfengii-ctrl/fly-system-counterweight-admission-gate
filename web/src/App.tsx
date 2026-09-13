import { FormEvent, useCallback, useEffect, useState } from 'react';
import { fetchBatten, fetchBattens, submitLoad } from './api';
import type { BattenDetail, BattenSummary } from './types';

interface Feedback {
  kind: 'success' | 'error';
  text: string;
}

export default function App() {
  const [battens, setBattens] = useState<BattenSummary[]>([]);
  const [selected, setSelected] = useState('G-01');
  const [detail, setDetail] = useState<BattenDetail | null>(null);
  const [pieceId, setPieceId] = useState('');
  const [weight, setWeight] = useState('');
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 页面状态永远以数据库为准：挂载与每次登记后都重新拉取
  const refresh = useCallback(async (battenId: string) => {
    const [list, det] = await Promise.all([fetchBattens(), fetchBatten(battenId)]);
    if (list.status === 200) {
      setBattens(list.body.battens);
    }
    if (det.status === 200) {
      setDetail(det.body);
    } else {
      setDetail(null);
    }
  }, []);

  useEffect(() => {
    void refresh(selected);
  }, [refresh, selected]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFeedback(null);
    const trimmedId = pieceId.trim();
    const grams = Number(weight);
    if (!trimmedId || weight.trim() === '' || !Number.isInteger(grams)) {
      setFeedback({ kind: 'error', text: '请输入配重片标识和整数克重量' });
      return;
    }
    setSubmitting(true);
    try {
      const { body } = await submitLoad(selected, trimmedId, grams);
      if (body.accepted) {
        setFeedback({
          kind: 'success',
          text: `${body.message}：当前总重 ${body.total_grams} 克，剩余量 ${body.remaining_grams} 克`,
        });
        setPieceId('');
        setWeight('');
      } else {
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    await refresh(selected);
  }

  return (
    <main className="page">
      <h1>剧场吊杆配重装载台</h1>

      <section aria-label="吊杆总览" className="cards">
        {battens.map((b) => (
          <button
            key={b.batten_id}
            type="button"
            className={b.batten_id === selected ? 'card selected' : 'card'}
            aria-pressed={b.batten_id === selected}
            onClick={() => setSelected(b.batten_id)}
          >
            <strong>{b.batten_id}</strong>
            <span>核定 {b.capacity_grams} 克</span>
            <span>总重 {b.total_grams} 克</span>
            <span>剩余 {b.remaining_grams} 克</span>
          </button>
        ))}
      </section>

      <form className="load-form" noValidate onSubmit={handleSubmit}>
        <label htmlFor="batten-select">选择吊杆</label>
        <select
          id="batten-select"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          {battens.map((b) => (
            <option key={b.batten_id} value={b.batten_id}>
              {b.batten_id}
            </option>
          ))}
        </select>

        <label htmlFor="piece-id">配重片标识</label>
        <input
          id="piece-id"
          value={pieceId}
          placeholder="例如 CW-0001"
          onChange={(e) => setPieceId(e.target.value)}
        />

        <label htmlFor="weight">重量（克）</label>
        <input
          id="weight"
          type="number"
          step="1"
          value={weight}
          placeholder="100 ~ 25000"
          onChange={(e) => setWeight(e.target.value)}
        />

        <button type="submit" disabled={submitting}>
          登记装载
        </button>
      </form>

      {feedback && (
        <p
          role={feedback.kind === 'error' ? 'alert' : 'status'}
          className={`feedback ${feedback.kind}`}
        >
          {feedback.text}
        </p>
      )}

      {detail && (
        <section aria-label="吊杆明细" className="detail">
          <h2>{detail.batten_id} 当前状态</h2>
          <dl className="stats">
            <div>
              <dt>核定重量</dt>
              <dd>{detail.capacity_grams} 克</dd>
            </div>
            <div>
              <dt>当前总重</dt>
              <dd data-testid="total">{detail.total_grams} 克</dd>
            </div>
            <div>
              <dt>剩余量</dt>
              <dd data-testid="remaining">{detail.remaining_grams} 克</dd>
            </div>
          </dl>
          {detail.loads.length === 0 ? (
            <p>暂无已接纳配重片</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>配重片标识</th>
                  <th>重量（克）</th>
                </tr>
              </thead>
              <tbody>
                {detail.loads.map((load) => (
                  <tr key={load.load_id}>
                    <td>{load.piece_id}</td>
                    <td>{load.weight_grams}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </main>
  );
}
