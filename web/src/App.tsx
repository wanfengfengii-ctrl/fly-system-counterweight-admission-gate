import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import {
  correctLoadWeight,
  fetchBatten,
  fetchBattens,
  fetchInspections,
  removeLoad,
  submitInspection,
  submitLoad,
  transferLoad,
} from './api';
import type {
  BattenDetail,
  BattenSummary,
  InspectionRecord,
  LoadItem,
} from './types';

interface Feedback {
  kind: 'success' | 'error';
  text: string;
}

// 本地营业日期（YYYY-MM-DD）：日检表单默认今天，未来日期由服务端拒绝
function todayStr(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}

export default function App() {
  const [battens, setBattens] = useState<BattenSummary[]>([]);
  const [selected, setSelected] = useState('G-01');
  const [detail, setDetail] = useState<BattenDetail | null>(null);
  const [pieceId, setPieceId] = useState('');
  const [weight, setWeight] = useState('');
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 待转移的配重片（来自当前吊杆明细）；null 表示未进入转移流程
  const [transferring, setTransferring] = useState<LoadItem | null>(null);
  const [transferTarget, setTransferTarget] = useState('');
  // 待修正重量的配重片（来自当前吊杆明细）；null 表示未进入修正流程
  const [correcting, setCorrecting] = useState<LoadItem | null>(null);
  const [correctionWeight, setCorrectionWeight] = useState('');
  // 待拆下的配重片（来自当前吊杆明细）；null 表示未进入拆下二次确认流程
  const [removing, setRemoving] = useState<LoadItem | null>(null);

  // —— 吊杆日检：独立的日检区域，状态全部来自接口 ——
  const [inspBatten, setInspBatten] = useState('G-01');
  const [inspDate, setInspDate] = useState(todayStr);
  const [brakeOk, setBrakeOk] = useState(true);
  const [ropeOk, setRopeOk] = useState(true);
  const [limitOk, setLimitOk] = useState(true);
  const [note, setNote] = useState('');
  // 本次提交的服务端归档结果（结论、明细、记录时间）；null 表示尚未提交
  const [inspResult, setInspResult] = useState<InspectionRecord | null>(null);
  const [inspFeedback, setInspFeedback] = useState<Feedback | null>(null);
  const [inspHistory, setInspHistory] = useState<InspectionRecord[]>([]);
  const [inspSubmitting, setInspSubmitting] = useState(false);
  // 与装载区同一思路：丢弃过期的在途历史查询响应
  const inspSeq = useRef(0);

  // 单调递增的刷新序号：放弃早于最新一次刷新返回的过期响应，
  // 避免上一个动作的在途拉取在新动作之后落地，把界面回滚成旧状态
  const refreshSeq = useRef(0);

  // 页面状态永远以数据库为准：挂载与每次登记/转移后都重新拉取
  const refresh = useCallback(async (battenId: string) => {
    const seq = ++refreshSeq.current;
    const [list, det] = await Promise.all([fetchBattens(), fetchBatten(battenId)]);
    // 丢弃在更早动作中发出、却晚于最新刷新落地的过期响应
    if (seq !== refreshSeq.current) return;
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

  // 日检历史永远以数据库为准：挂载、切换日检吊杆与每次提交后都重新拉取
  const refreshInspections = useCallback(async (battenId: string) => {
    const seq = ++inspSeq.current;
    const res = await fetchInspections(battenId);
    if (seq !== inspSeq.current) return;
    setInspHistory(res.status === 200 ? res.body.inspections : []);
  }, []);

  useEffect(() => {
    // 切换日检吊杆后，上一次的结果与提示不再适用于当前吊杆
    setInspResult(null);
    setInspFeedback(null);
    void refreshInspections(inspBatten);
  }, [inspBatten, refreshInspections]);

  // 切换吊杆后，上一根吊杆的待转移 / 待修正 / 待拆下配重片不再适用于当前明细
  useEffect(() => {
    setTransferring(null);
    setTransferTarget('');
    setCorrecting(null);
    setCorrectionWeight('');
    setRemoving(null);
  }, [selected]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFeedback(null);
    const trimmedId = pieceId.trim();
    const trimmedWeight = weight.trim();
    // 按原始输入严格判定整数：字符串 "100.0"、"1e2" 等一律在页面明确拒绝
    if (!trimmedId || !/^-?\d+$/.test(trimmedWeight)) {
      setFeedback({ kind: 'error', text: '请输入配重片标识，重量必须是整数克数' });
      return;
    }
    const grams = Number(trimmedWeight);
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

  function beginTransfer(load: LoadItem) {
    setFeedback(null);
    // 转移、修正与拆下互斥：同一时间只针对一片配重片操作
    setCorrecting(null);
    setCorrectionWeight('');
    setRemoving(null);
    setTransferring(load);
    // 默认目标为另一根吊杆
    setTransferTarget(battens.find((b) => b.batten_id !== selected)?.batten_id ?? '');
  }

  function cancelTransfer() {
    setTransferring(null);
    setTransferTarget('');
  }

  async function confirmTransfer() {
    if (!transferring || !transferTarget) return;
    setFeedback(null);
    setSubmitting(true);
    try {
      const { body } = await transferLoad(
        selected,
        transferring.load_id,
        transferTarget,
      );
      if (body.accepted) {
        setFeedback({
          kind: 'success',
          text: `${body.message}：${body.source_batten_id} 总重 ${body.source?.total_grams ?? ''} 克、剩余 ${body.source?.remaining_grams ?? ''} 克；${body.target_batten_id} 总重 ${body.target?.total_grams ?? ''} 克、剩余 ${body.target?.remaining_grams ?? ''} 克`,
        });
        setTransferring(null);
        setTransferTarget('');
      } else {
        // 当前位置已变化等拒绝后，明细需要以数据库为准刷新
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
        setTransferring(null);
        setTransferTarget('');
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    // 无论成功或拒绝，都刷新两根吊杆总重、余量与明细
    await refresh(selected);
  }

  function beginCorrect(load: LoadItem) {
    setFeedback(null);
    // 修正、转移与拆下互斥：同一时间只针对一片配重片操作
    setTransferring(null);
    setTransferTarget('');
    setRemoving(null);
    setCorrecting(load);
    setCorrectionWeight('');
  }

  function cancelCorrect() {
    setCorrecting(null);
    setCorrectionWeight('');
  }

  function beginRemove(load: LoadItem) {
    setFeedback(null);
    // 拆下、转移与修正互斥：同一时间只针对一片配重片操作
    setTransferring(null);
    setTransferTarget('');
    setCorrecting(null);
    setCorrectionWeight('');
    setRemoving(load);
  }

  function cancelRemove() {
    setRemoving(null);
  }

  async function confirmRemove() {
    if (!removing) return;
    setFeedback(null);
    setSubmitting(true);
    try {
      const { body } = await removeLoad(selected, removing.load_id);
      if (body.accepted) {
        const releaseNote = body.already_removed
          ? '本次释放 0 克（已拆下，容量未重复释放）'
          : `本次释放 ${body.released_grams} 克`;
        setFeedback({
          kind: 'success',
          text: `${body.message}；${releaseNote}：当前总重 ${body.total_grams} 克，剩余量 ${body.remaining_grams} 克`,
        });
      } else {
        // 当前位置已变化（已被其他终端转移）：明细以数据库为准刷新
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    setRemoving(null);
    // 无论成功、重复拆下或拒绝，都刷新总重、余量与在杆明细
    await refresh(selected);
  }

  async function confirmCorrect() {
    if (!correcting) return;
    setFeedback(null);
    const trimmed = correctionWeight.trim();
    // 与登记同样的严格整数判定：非整数留在面板中直接修改
    if (!/^-?\d+$/.test(trimmed)) {
      setFeedback({ kind: 'error', text: '新重量必须是整数克数' });
      return;
    }
    setSubmitting(true);
    try {
      const { body } = await correctLoadWeight(
        selected,
        correcting.load_id,
        Number(trimmed),
      );
      if (body.accepted) {
        setFeedback({
          kind: 'success',
          text: `${body.message}：当前总重 ${body.total_grams} 克，剩余量 ${body.remaining_grams} 克`,
        });
      } else {
        // 当前位置已变化 / 超载 / 越界等拒绝：明细以数据库为准刷新
        setFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
      }
    } catch {
      setFeedback({ kind: 'error', text: '网络错误，无法联系装载裁决服务' });
    } finally {
      setSubmitting(false);
    }
    setCorrecting(null);
    setCorrectionWeight('');
    // 无论成功或拒绝，都刷新该杆总重、余量与明细
    await refresh(selected);
  }

  async function handleInspectionSubmit(event: FormEvent) {
    event.preventDefault();
    setInspFeedback(null);
    setInspResult(null);
    if (!inspDate) {
      setInspFeedback({ kind: 'error', text: '请选择营业日期' });
      return;
    }
    // 页面预检与接口口径一致：任一项异常时说明不能为空；
    // 结论不由页面判定，始终以服务端归档结果为准
    const anyAbnormal = !brakeOk || !ropeOk || !limitOk;
    const trimmedNote = note.trim();
    if (anyAbnormal && !trimmedNote) {
      setInspFeedback({
        kind: 'error',
        text: '存在异常检查项时，异常说明不能为空',
      });
      return;
    }
    setInspSubmitting(true);
    try {
      const { body } = await submitInspection(inspBatten, {
        inspection_date: inspDate,
        brake_ok: brakeOk,
        rope_ok: ropeOk,
        limit_ok: limitOk,
        abnormality_note: trimmedNote || null,
      });
      if (body.accepted && body.inspection) {
        setInspResult(body.inspection);
        setInspFeedback({ kind: 'success', text: body.message });
        // 复位为全部正常，便于下一根吊杆 / 下一个营业日的登记
        setBrakeOk(true);
        setRopeOk(true);
        setLimitOk(true);
        setNote('');
      } else {
        // 已完成 / 未来日期 / 缺少说明等拒绝：历史以数据库为准刷新
        setInspFeedback({ kind: 'error', text: `已拒绝：${body.message}` });
      }
    } catch {
      setInspFeedback({ kind: 'error', text: '网络错误，无法联系日检服务' });
    } finally {
      setInspSubmitting(false);
    }
    // 无论成功或拒绝，历史视图都重新拉取接口
    await refreshInspections(inspBatten);
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
          type="text"
          inputMode="numeric"
          value={weight}
          placeholder="100 ~ 25000 的整数"
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
                  <th>登记时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {detail.loads.map((load) => (
                  <tr key={load.load_id}>
                    <td>{load.piece_id}</td>
                    <td>{load.weight_grams}</td>
                    <td>
                      {load.created_at
                        ? new Date(load.created_at).toLocaleString('zh-CN', {
                            hour12: false,
                          })
                        : ''}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="correct-btn"
                        aria-label={`修正重量 ${load.piece_id}`}
                        onClick={() => beginCorrect(load)}
                        disabled={submitting}
                      >
                        修正重量
                      </button>
                      <button
                        type="button"
                        className="transfer-btn"
                        aria-label={`转移 ${load.piece_id}`}
                        onClick={() => beginTransfer(load)}
                        disabled={submitting}
                      >
                        转移
                      </button>
                      <button
                        type="button"
                        className="remove-btn"
                        aria-label={`拆下 ${load.piece_id}`}
                        onClick={() => beginRemove(load)}
                        disabled={submitting}
                      >
                        确认拆下
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {correcting && (
            <div className="correct-panel" role="group" aria-label="修正重量">
              <p>
                修正配重片 <strong>{correcting.piece_id}</strong> 的标称重量（当前{' '}
                {correcting.weight_grams} 克，标识与登记时间保留）：
              </p>
              <label htmlFor="correct-weight">新重量（克）</label>
              <input
                id="correct-weight"
                type="text"
                inputMode="numeric"
                value={correctionWeight}
                placeholder="100 ~ 25000 的整数"
                onChange={(e) => setCorrectionWeight(e.target.value)}
              />
              <button
                type="button"
                className="confirm-correct"
                disabled={submitting}
                onClick={() => void confirmCorrect()}
              >
                确认修正
              </button>
              <button
                type="button"
                className="cancel-correct"
                disabled={submitting}
                onClick={cancelCorrect}
              >
                取消
              </button>
            </div>
          )}

          {transferring && (
            <div className="transfer-panel" role="group" aria-label="转移确认">
              <p>
                将配重片 <strong>{transferring.piece_id}</strong>（
                {transferring.weight_grams} 克）从 {selected} 转移到：
              </p>
              <label htmlFor="transfer-target">目标吊杆</label>
              <select
                id="transfer-target"
                value={transferTarget}
                onChange={(e) => setTransferTarget(e.target.value)}
              >
                {battens
                  .filter((b) => b.batten_id !== selected)
                  .map((b) => (
                    <option key={b.batten_id} value={b.batten_id}>
                      {b.batten_id}（剩余 {b.remaining_grams} 克）
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="confirm-transfer"
                disabled={submitting || !transferTarget}
                onClick={() => void confirmTransfer()}
              >
                确认转移
              </button>
              <button
                type="button"
                className="cancel-transfer"
                disabled={submitting}
                onClick={cancelTransfer}
              >
                取消
              </button>
            </div>
          )}

          {removing && (
            <div className="remove-panel" role="group" aria-label="拆下确认">
              <p>
                确认从 {selected} 拆下配重片 <strong>{removing.piece_id}</strong>（
                {removing.weight_grams} 克）？拆下后容量立即释放，但该片的标识、
                重量、归属与登记时间仍保留备查；标识不能再次登记。
              </p>
              <button
                type="button"
                className="confirm-remove"
                disabled={submitting}
                onClick={() => void confirmRemove()}
              >
                确认拆下
              </button>
              <button
                type="button"
                className="cancel-remove"
                disabled={submitting}
                onClick={cancelRemove}
              >
                取消
              </button>
            </div>
          )}
        </section>
      )}

      <section aria-label="吊杆日检" className="inspection">
        <h2>吊杆日检</h2>
        <form className="inspection-form" noValidate onSubmit={handleInspectionSubmit}>
          <label htmlFor="insp-batten">日检吊杆</label>
          <select
            id="insp-batten"
            value={inspBatten}
            onChange={(e) => setInspBatten(e.target.value)}
          >
            {battens.map((b) => (
              <option key={b.batten_id} value={b.batten_id}>
                {b.batten_id}
              </option>
            ))}
          </select>

          <label htmlFor="insp-date">营业日期</label>
          <input
            id="insp-date"
            type="date"
            value={inspDate}
            max={todayStr()}
            onChange={(e) => setInspDate(e.target.value)}
          />

          <span id="checks-label" className="checks-label">
            检查项
          </span>
          <div className="checks" role="group" aria-labelledby="checks-label">
            <label className="check" htmlFor="insp-brake">
              <input
                id="insp-brake"
                type="checkbox"
                checked={brakeOk}
                onChange={(e) => setBrakeOk(e.target.checked)}
              />
              制动器正常
            </label>
            <label className="check" htmlFor="insp-rope">
              <input
                id="insp-rope"
                type="checkbox"
                checked={ropeOk}
                onChange={(e) => setRopeOk(e.target.checked)}
              />
              钢丝绳正常
            </label>
            <label className="check" htmlFor="insp-limit">
              <input
                id="insp-limit"
                type="checkbox"
                checked={limitOk}
                onChange={(e) => setLimitOk(e.target.checked)}
              />
              限位装置正常
            </label>
          </div>

          <label htmlFor="insp-note">异常说明</label>
          <textarea
            id="insp-note"
            rows={2}
            value={note}
            placeholder="任一项异常时必填"
            onChange={(e) => setNote(e.target.value)}
          />

          <button type="submit" disabled={inspSubmitting}>
            提交日检
          </button>
        </form>

        {inspFeedback && (
          <p
            role={inspFeedback.kind === 'error' ? 'alert' : 'status'}
            className={`feedback ${inspFeedback.kind}`}
          >
            {inspFeedback.text}
          </p>
        )}

        {inspResult && (
          <section aria-label="日检结果" className="inspection-result">
            <h3>本次日检结果</h3>
            <p>
              结论：
              <strong data-testid="insp-conclusion">
                {inspResult.conclusion_label}
              </strong>
            </p>
            <ul className="check-results">
              <li>制动器：{inspResult.brake_ok ? '正常' : '异常'}</li>
              <li>钢丝绳：{inspResult.rope_ok ? '正常' : '异常'}</li>
              <li>限位装置：{inspResult.limit_ok ? '正常' : '异常'}</li>
            </ul>
            {inspResult.abnormality_note && (
              <p>异常说明：{inspResult.abnormality_note}</p>
            )}
            <p>
              记录时间：
              {inspResult.created_at
                ? new Date(inspResult.created_at).toLocaleString('zh-CN', {
                    hour12: false,
                  })
                : ''}
            </p>
          </section>
        )}

        <section aria-label="日检记录" className="inspection-history">
          <h3>{inspBatten} 最近日检记录</h3>
          {inspHistory.length === 0 ? (
            <p>暂无日检记录</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>营业日期</th>
                  <th>结论</th>
                  <th>制动器</th>
                  <th>钢丝绳</th>
                  <th>限位装置</th>
                  <th>异常说明</th>
                  <th>记录时间</th>
                </tr>
              </thead>
              <tbody>
                {inspHistory.map((rec) => (
                  <tr key={rec.inspection_id}>
                    <td>{rec.inspection_date}</td>
                    <td>{rec.conclusion_label}</td>
                    <td>{rec.brake_ok ? '正常' : '异常'}</td>
                    <td>{rec.rope_ok ? '正常' : '异常'}</td>
                    <td>{rec.limit_ok ? '正常' : '异常'}</td>
                    <td>{rec.abnormality_note ?? '—'}</td>
                    <td>
                      {rec.created_at
                        ? new Date(rec.created_at).toLocaleString('zh-CN', {
                            hour12: false,
                          })
                        : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </section>
    </main>
  );
}
