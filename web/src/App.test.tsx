import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import App from './App';

// 真实接口地址由 vite.config.ts 的 test.env 注入（容器内为 http://api:8000）
const BASE: string = import.meta.env.VITE_API_URL ?? '';

async function resetStage() {
  const res = await fetch(`${BASE}/api/reset`, { method: 'POST' });
  if (!res.ok) throw new Error(`reset 失败: ${res.status}`);
}

async function dbBatten(battenId: string) {
  const res = await fetch(`${BASE}/api/battens/${battenId}`);
  if (!res.ok) throw new Error(`查询吊杆失败: ${res.status}`);
  return res.json();
}

async function renderLoaded() {
  const user = userEvent.setup();
  render(<App />);
  // 等待真实接口返回、两根吊杆渲染完成
  await screen.findByRole('button', { name: /G-01/ });
  await screen.findByRole('button', { name: /G-02/ });
  return user;
}

async function submitPiece(
  user: ReturnType<typeof userEvent.setup>,
  pieceId: string,
  weight: string,
) {
  await user.clear(screen.getByLabelText('配重片标识'));
  await user.type(screen.getByLabelText('配重片标识'), pieceId);
  await user.clear(screen.getByLabelText('重量（克）'));
  await user.type(screen.getByLabelText('重量（克）'), weight);
  await user.click(screen.getByRole('button', { name: '登记装载' }));
}

beforeEach(resetStage);
afterEach(cleanup);

describe('吊杆配重装载页（真实接口反馈）', () => {
  it('页面加载后展示两根预置吊杆，且与数据库状态一致', async () => {
    await renderLoaded();

    const g01 = screen.getByRole('button', { name: /G-01/ });
    const g02 = screen.getByRole('button', { name: /G-02/ });
    expect(within(g01).getByText('核定 30000 克')).toBeInTheDocument();
    expect(within(g01).getByText('总重 0 克')).toBeInTheDocument();
    expect(within(g01).getByText('剩余 30000 克')).toBeInTheDocument();
    expect(within(g02).getByText('核定 50000 克')).toBeInTheDocument();

    // 默认选中 G-01，明细区展示空吊杆
    expect(await screen.findByTestId('total')).toHaveTextContent('0 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('30000 克');
    expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument();

    // 与数据库直接核对
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.remaining_grams).toBe(30000);
  });

  it('合法配重片被接纳，页面与数据库同步更新', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-0001', '20000');

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已接纳');
    expect(status).toHaveTextContent('当前总重 20000 克');
    expect(status).toHaveTextContent('剩余量 10000 克');

    // 提交后页面会重新拉取接口，等待明细区刷新到最新状态
    await waitFor(() => expect(screen.getByTestId('total')).toHaveTextContent('20000 克'));
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');
    expect(screen.getByRole('cell', { name: 'CW-0001' })).toBeInTheDocument();

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(20000);
    expect(db.remaining_grams).toBe(10000);
    expect(db.loads.map((l: { piece_id: string }) => l.piece_id)).toEqual(['CW-0001']);
  });

  it('恰好装满核定值时允许接纳', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-FULL-1', '25000');
    await screen.findByRole('status');

    await submitPiece(user, 'CW-FULL-2', '5000');
    const status = await screen.findByRole('status');
    await waitFor(() => expect(status).toHaveTextContent('剩余量 0 克'));

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(30000);
    expect(db.remaining_grams).toBe(0);
  });

  it('超出核定值被明确拒绝，且失败请求不落库', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-KEEP', '20000');
    await screen.findByRole('status');

    await submitPiece(user, 'CW-OVER', '20000');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 页面状态保持第一笔之后的样子
    await waitFor(() => expect(screen.getByTestId('total')).toHaveTextContent('20000 克'));
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 数据库里只有成功的那一笔
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(20000);
    expect(db.loads).toHaveLength(1);
    expect(db.loads[0].piece_id).toBe('CW-KEEP');
  });

  it('同一标识在全库只能成功一次', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-DUP', '5000');
    await screen.findByRole('status');

    // 换到 G-02 再用同一标识登记
    await user.selectOptions(screen.getByLabelText('选择吊杆'), 'G-02');
    await waitFor(() =>
      expect(screen.getByTestId('remaining')).toHaveTextContent('50000 克'),
    );

    await submitPiece(user, 'CW-DUP', '5000');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('不能重复使用');

    const db = await dbBatten('G-02');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('重量超出 100～25000 克范围被明确拒绝', async () => {
    const user = await renderLoaded();

    await submitPiece(user, 'CW-LIGHT', '50');
    expect(await screen.findByRole('alert')).toHaveTextContent('100～25000');

    await submitPiece(user, 'CW-HEAVY', '26000');
    // 第二次拒绝会替换提示内容，等待新文案出现
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('收到 26000 克'),
    );
    expect(screen.getByRole('alert')).toHaveTextContent('100～25000');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('小数或非整数输入在页面被明确拒绝且不保存', async () => {
    const user = await renderLoaded();

    await submitPiece(user, 'CW-DEC', '100.0');
    expect(await screen.findByRole('alert')).toHaveTextContent('整数克数');

    await submitPiece(user, 'CW-ABC', 'abc');
    expect(await screen.findByRole('alert')).toHaveTextContent('整数克数');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('接口严格拒绝字符串或小数重量（真实接口）', async () => {
    // 手工构造原始 JSON 报文：JS 的 JSON.stringify(100.0) 会变成整数 100
    const badBodies = [
      '{"piece_id":"CW-STRICT-S1","weight_grams":"100"}',
      '{"piece_id":"CW-STRICT-S2","weight_grams":100.0}',
      '{"piece_id":"CW-STRICT-S3","weight_grams":"20000"}',
    ];
    for (const raw of badBodies) {
      const res = await fetch(`${BASE}/api/battens/G-01/loads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: raw,
      });
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.accepted).toBe(false);
      expect(body.reason).toBe('INVALID_INPUT');
    }

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.loads).toHaveLength(0);
  });

  it('页面重新渲染（刷新）后状态仍与数据库一致', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-PERSIST', '12000');
    await screen.findByRole('status');
    cleanup();

    // 模拟刷新：全新渲染，数据只能来自接口
    render(<App />);
    expect(await screen.findByTestId('total')).toHaveTextContent('12000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('18000 克');
    expect(screen.getByRole('cell', { name: 'CW-PERSIST' })).toBeInTheDocument();

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(12000);
    expect(db.remaining_grams).toBe(18000);
  });

  it('页面转移配重片：选择目标吊杆确认后，源杆与目标杆均刷新', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-MOVE', '20000');
    await screen.findByRole('status');

    // 先从数据库记录原始登记信息，用于核对转移后重量与登记时间保留
    const before = await dbBatten('G-01');
    const movedBefore = before.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-MOVE',
    );
    expect(movedBefore).toBeTruthy();

    // 当前吊杆明细中为每片显示“转移”
    const transferBtn = screen.getByRole('button', { name: '转移 CW-MOVE' });
    await user.click(transferBtn);

    // 确认面板出现，目标吊杆默认是另一根 G-02
    const panel = screen.getByRole('group', { name: '转移确认' });
    expect(panel).toBeInTheDocument();
    const targetSelect = screen.getByLabelText('目标吊杆');
    expect((targetSelect as HTMLSelectElement).value).toBe('G-02');

    await user.click(screen.getByRole('button', { name: '确认转移' }));

    // 成功提示给出配重片去向与两杆刷新后的总重 / 余量
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已从 G-01 转移至 G-02');
    expect(status).toHaveTextContent('G-01 总重 0 克');
    expect(status).toHaveTextContent('剩余 30000 克');
    expect(status).toHaveTextContent('G-02 总重 20000 克');
    expect(status).toHaveTextContent('剩余 30000 克');

    // 刷新后的源杆：总重 / 余量归零状态，明细中不再有该片。
    // 用整串正则避免 "0 克" 误匹配到转移前的 "20000 克"（子串包含）。
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent(/^0 克$/),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent(/^30000 克$/);
    expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument();
    expect(
      screen.queryByRole('cell', { name: 'CW-MOVE' }),
    ).not.toBeInTheDocument();

    // 直接与数据库核对：源杆已空，目标杆挂有同一笔记录（同一 load_id），
    // 原始重量与登记时间保留
    const src = await dbBatten('G-01');
    expect(src.total_grams).toBe(0);
    expect(src.remaining_grams).toBe(30000);
    expect(src.loads).toHaveLength(0);

    const dst = await dbBatten('G-02');
    expect(dst.total_grams).toBe(20000);
    expect(dst.remaining_grams).toBe(30000);
    expect(dst.loads).toHaveLength(1);
    const movedAfter = dst.loads[0];
    expect(movedAfter.load_id).toBe(movedBefore.load_id);
    expect(movedAfter.piece_id).toBe('CW-MOVE');
    expect(movedAfter.weight_grams).toBe(20000);
    expect(movedAfter.created_at).toBe(movedBefore.created_at);

    // 页面切到目标杆后，明细同样来自接口并显示该片
    await user.selectOptions(screen.getByLabelText('选择吊杆'), 'G-02');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-MOVE' })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('total')).toHaveTextContent('20000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('30000 克');
  });

  it('目标吊杆余量不足时转移被拒绝，页面与数据库均保持原归属', async () => {
    // 直接经接口布阵：G-01 挂 20000 克，G-02 已占 35000 克（余 15000 克）
    await fetch(`${BASE}/api/battens/G-01/loads`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ piece_id: 'CW-STAY', weight_grams: 20000 }),
    });
    for (const [id, w] of [
      ['CW-OCC-A', 20000],
      ['CW-OCC-B', 15000],
    ] as const) {
      await fetch(`${BASE}/api/battens/G-02/loads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ piece_id: id, weight_grams: w }),
      });
    }

    const user = await renderLoaded();
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-STAY' })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('button', { name: '转移 CW-STAY' }));
    await user.click(screen.getByRole('button', { name: '确认转移' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 源杆明细不变
    expect(screen.getByRole('cell', { name: 'CW-STAY' })).toBeInTheDocument();
    expect(screen.getByTestId('total')).toHaveTextContent('20000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 数据库保持原归属
    const src = await dbBatten('G-01');
    expect(src.loads.map((l: { piece_id: string }) => l.piece_id)).toEqual([
      'CW-STAY',
    ]);
    const dst = await dbBatten('G-02');
    expect(dst.total_grams).toBe(35000);
    expect(dst.remaining_grams).toBe(15000);
  });

  it('页面修正重量：超载被拒绝并提示，合法修正成功后总重 / 余量 / 明细刷新', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-FIX-A', '20000');
    await screen.findByRole('status');
    await submitPiece(user, 'CW-FIX-B', '10000');
    // G-01 恰好满载 30000 克
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent('30000 克'),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent(/^0 克$/);

    // 记录 CW-FIX-B 的原始登记信息，用于核对修正后标识与登记时间保留
    const before = await dbBatten('G-01');
    const fixBefore = before.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-FIX-B',
    );
    expect(fixBefore).toBeTruthy();

    // 超载修正：CW-FIX-A 20000 → 20001，合计 30001 超出核定，必须拒绝
    await user.click(screen.getByRole('button', { name: '修正重量 CW-FIX-A' }));
    const panel = screen.getByRole('group', { name: '修正重量' });
    expect(panel).toHaveTextContent('当前 20000 克');
    await user.type(screen.getByLabelText('新重量（克）'), '20001');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('超出核定');

    // 拒绝后页面与数据库都保持原重量
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent('30000 克'),
    );
    let db = await dbBatten('G-01');
    expect(db.total_grams).toBe(30000);
    expect(
      db.loads.find((l: { piece_id: string }) => l.piece_id === 'CW-FIX-A')
        .weight_grams,
    ).toBe(20000);

    // 合法修正：CW-FIX-B 10000 → 5000，总重降至 25000 克
    await user.click(screen.getByRole('button', { name: '修正重量 CW-FIX-B' }));
    await user.type(screen.getByLabelText('新重量（克）'), '5000');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    // 明确展示修正结果：新旧重量与刷新后的总重 / 余量
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已从 10000 克修正为 5000 克');
    expect(status).toHaveTextContent('当前总重 25000 克');
    expect(status).toHaveTextContent('剩余量 5000 克');

    // 页面重新拉取接口：总重、余量与明细行都刷新
    await waitFor(() =>
      expect(screen.getByTestId('total')).toHaveTextContent('25000 克'),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent('5000 克');
    const row = screen.getByRole('row', { name: /CW-FIX-B/ });
    expect(within(row).getByText('5000')).toBeInTheDocument();

    // 与数据库核对：同一笔记录只改了重量，标识与最初登记时间保留
    db = await dbBatten('G-01');
    expect(db.total_grams).toBe(25000);
    expect(db.remaining_grams).toBe(5000);
    const fixAfter = db.loads.find(
      (l: { piece_id: string }) => l.piece_id === 'CW-FIX-B',
    );
    expect(fixAfter.load_id).toBe(fixBefore.load_id);
    expect(fixAfter.weight_grams).toBe(5000);
    expect(fixAfter.created_at).toBe(fixBefore.created_at);
  });

  it('配重片已被其他终端转移时，修正提示当前位置已变化并重新加载明细', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-STALE', '5000');
    await screen.findByRole('status');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-STALE' })).toBeInTheDocument(),
    );

    // 另一终端直接把片子转移到 G-02，页面明细随之过期
    const before = await dbBatten('G-01');
    const loadId = before.loads[0].load_id;
    const moved = await fetch(
      `${BASE}/api/battens/G-01/loads/${loadId}/transfer`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_batten_id: 'G-02' }),
      },
    );
    expect(moved.status).toBe(200);

    // 旧页面仍显示该片，技师照常在当前吊杆明细里提交修正
    await user.click(screen.getByRole('button', { name: '修正重量 CW-STALE' }));
    await user.type(screen.getByLabelText('新重量（克）'), '8000');
    await user.click(screen.getByRole('button', { name: '确认修正' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('当前位置已变化');

    // 明细重新加载后与数据库一致：G-01 已空，该片在 G-02 保持原重量
    await waitFor(() =>
      expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('cell', { name: 'CW-STALE' }),
    ).not.toBeInTheDocument();

    const src = await dbBatten('G-01');
    expect(src.loads).toHaveLength(0);
    const dst = await dbBatten('G-02');
    expect(dst.loads).toHaveLength(1);
    expect(dst.loads[0].piece_id).toBe('CW-STALE');
    expect(dst.loads[0].weight_grams).toBe(5000);
  });

  it('确认拆下：二次确认后明细消失、余量增加并展示本次释放重量', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-OFF', '20000');
    await screen.findByRole('status');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-OFF' })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 第一步：点击明细行的“确认拆下”，仅弹出二次确认，尚未真正拆下
    await user.click(screen.getByRole('button', { name: '拆下 CW-OFF' }));
    const panel = screen.getByRole('group', { name: '拆下确认' });
    expect(panel).toBeInTheDocument();
    expect(panel).toHaveTextContent('CW-OFF');
    expect(panel).toHaveTextContent('20000 克');
    // 确认前明细仍在、容量未释放
    expect(screen.getByRole('cell', { name: 'CW-OFF' })).toBeInTheDocument();
    expect(screen.getByTestId('remaining')).toHaveTextContent('10000 克');

    // 第二步：在确认面板中再次点击“确认拆下”
    await user.click(screen.getByRole('button', { name: '确认拆下' }));

    // 成功反馈明确给出本次释放的重量与刷新后的总重 / 余量
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('已确认从 G-01 拆下');
    expect(status).toHaveTextContent('释放容量 20000 克');
    expect(status).toHaveTextContent('本次释放 20000 克');
    expect(status).toHaveTextContent('当前总重 0 克');
    expect(status).toHaveTextContent('剩余量 30000 克');

    // 在杆明细刷新后该片消失，余量回升
    await waitFor(() =>
      expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('cell', { name: 'CW-OFF' }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('total')).toHaveTextContent(/^0 克$/);
    expect(screen.getByTestId('remaining')).toHaveTextContent(/^30000 克$/);

    // 直接与数据库核对：容量已释放、明细已空
    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(0);
    expect(db.remaining_grams).toBe(30000);
    expect(db.loads).toHaveLength(0);

    // 历史唯一标识仍被占用：已拆下的标识不能再次登记
    const reused = await fetch(`${BASE}/api/battens/G-02/loads`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ piece_id: 'CW-OFF', weight_grams: 1000 }),
    });
    expect(reused.status).toBe(409);
    expect((await reused.json()).reason).toBe('PIECE_ID_EXISTS');
  });

  it('拆下二次确认面板点取消：配重片保留在明细，容量不释放', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-KEEP-ON', '8000');
    await screen.findByRole('status');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-KEEP-ON' })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('button', { name: '拆下 CW-KEEP-ON' }));
    expect(screen.getByRole('group', { name: '拆下确认' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '取消' }));

    // 面板关闭，配重片仍在杆上，总重 / 余量不变
    expect(
      screen.queryByRole('group', { name: '拆下确认' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'CW-KEEP-ON' })).toBeInTheDocument();
    expect(screen.getByTestId('total')).toHaveTextContent('8000 克');
    expect(screen.getByTestId('remaining')).toHaveTextContent('22000 克');

    const db = await dbBatten('G-01');
    expect(db.total_grams).toBe(8000);
    expect(db.loads).toHaveLength(1);
  });

  it('配重片已被其他终端转移时，拆下提示当前位置已变化并重载明细', async () => {
    const user = await renderLoaded();
    await submitPiece(user, 'CW-RM-STALE', '5000');
    await screen.findByRole('status');
    await waitFor(() =>
      expect(screen.getByRole('cell', { name: 'CW-RM-STALE' })).toBeInTheDocument(),
    );

    // 另一终端先把片子转移到 G-02，页面明细随之过期
    const before = await dbBatten('G-01');
    const loadId = before.loads[0].load_id;
    const moved = await fetch(
      `${BASE}/api/battens/G-01/loads/${loadId}/transfer`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_batten_id: 'G-02' }),
      },
    );
    expect(moved.status).toBe(200);

    // 旧页面仍显示该片，技师照常在当前吊杆明细里确认拆下
    await user.click(screen.getByRole('button', { name: '拆下 CW-RM-STALE' }));
    await user.click(screen.getByRole('button', { name: '确认拆下' }));

    // 并发失败反馈：当前位置已变化
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('当前位置已变化');

    // 明细重新加载后与数据库一致：G-01 已空，该片在 G-02 保持原重量，
    // 源杆容量未被错误释放
    await waitFor(() =>
      expect(screen.getByText('暂无已接纳配重片')).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('cell', { name: 'CW-RM-STALE' }),
    ).not.toBeInTheDocument();

    const src = await dbBatten('G-01');
    expect(src.loads).toHaveLength(0);
    expect(src.total_grams).toBe(0);
    const dst = await dbBatten('G-02');
    expect(dst.loads).toHaveLength(1);
    expect(dst.loads[0].piece_id).toBe('CW-RM-STALE');
    expect(dst.loads[0].weight_grams).toBe(5000);
    expect(dst.total_grams).toBe(5000);
  });
});

describe('吊杆日检（真实接口反馈）', () => {
  // 与页面 todayStr() 同一口径的本地营业日期
  function localTodayStr(): string {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${mm}-${dd}`;
  }

  it('异常日检提交后展示服务端结论，历史视图与刷新后均来自数据库', async () => {
    const user = await renderLoaded();

    // 默认日检吊杆 G-01、默认营业日期今天；把钢丝绳勾选为异常并填写说明
    await user.click(screen.getByLabelText('钢丝绳正常'));
    await user.type(screen.getByLabelText('异常说明'), '钢丝绳发现断丝，需更换');
    await user.click(screen.getByRole('button', { name: '提交日检' }));

    // 成功反馈给出服务端判定的结论（需处理），而非页面自行判定
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('日检已归档');
    expect(status).toHaveTextContent('需处理');

    // 结果区展示结论、检查明细与记录时间
    const result = screen.getByRole('region', { name: '日检结果' });
    expect(result).toHaveTextContent('需处理');
    expect(result).toHaveTextContent('制动器：正常');
    expect(result).toHaveTextContent('钢丝绳：异常');
    expect(result).toHaveTextContent('限位装置：正常');
    expect(result).toHaveTextContent('钢丝绳发现断丝，需更换');
    expect(result).toHaveTextContent('记录时间：');

    // 历史视图出现该记录，结论来自服务端
    const history = screen.getByRole('region', { name: '日检记录' });
    await waitFor(() =>
      expect(history).toHaveTextContent('钢丝绳发现断丝，需更换'),
    );
    expect(history).toHaveTextContent('需处理');
    expect(history).toHaveTextContent(localTodayStr());

    // 直接与数据库核对：结论由服务端判定为需处理，明细与说明已归档
    const res = await fetch(`${BASE}/api/battens/G-01/inspections`);
    expect(res.ok).toBe(true);
    const db = await res.json();
    expect(db.inspections).toHaveLength(1);
    expect(db.inspections[0].inspection_date).toBe(localTodayStr());
    expect(db.inspections[0].conclusion).toBe('NEEDS_ATTENTION');
    expect(db.inspections[0].conclusion_label).toBe('需处理');
    expect(db.inspections[0].brake_ok).toBe(true);
    expect(db.inspections[0].rope_ok).toBe(false);
    expect(db.inspections[0].limit_ok).toBe(true);
    expect(db.inspections[0].abnormality_note).toBe('钢丝绳发现断丝，需更换');
    expect(db.inspections[0].created_at).toBeTruthy();

    // 模拟刷新：全新渲染，历史记录仍来自数据库
    cleanup();
    render(<App />);
    const historyAfter = await screen.findByRole('region', { name: '日检记录' });
    await waitFor(() =>
      expect(historyAfter).toHaveTextContent('钢丝绳发现断丝，需更换'),
    );
    expect(historyAfter).toHaveTextContent('需处理');
    expect(historyAfter).toHaveTextContent(localTodayStr());
  });

  it('任一项异常而说明为空时页面预检拦截，不产生巡检记录', async () => {
    const user = await renderLoaded();

    await user.click(screen.getByLabelText('制动器正常'));
    // 不填异常说明直接提交
    await user.click(screen.getByRole('button', { name: '提交日检' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('异常说明不能为空');

    // 数据库中没有留下任何巡检记录
    const res = await fetch(`${BASE}/api/battens/G-01/inspections`);
    const db = await res.json();
    expect(db.inspections).toHaveLength(0);
  });

  it('只填零宽空格作为异常说明时被明确拒绝，不产生巡检记录', async () => {
    const user = await renderLoaded();

    await user.click(screen.getByLabelText('钢丝绳正常'));
    // 零宽空格（U+200B）看起来为空，不能算作异常说明
    fireEvent.change(screen.getByLabelText('异常说明'), {
      target: { value: '\u200b\u200b' },
    });
    await user.click(screen.getByRole('button', { name: '提交日检' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('异常说明不能为空');

    // 服务端口径一致：绕过页面预检直接提交零宽空格说明同样被拒绝
    const direct = await fetch(`${BASE}/api/battens/G-01/inspections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        inspection_date: localTodayStr(),
        brake_ok: false,
        rope_ok: true,
        limit_ok: true,
        abnormality_note: '\u200b',
      }),
    });
    expect(direct.status).toBe(422);
    expect((await direct.json()).reason).toBe('MISSING_ABNORMALITY_NOTE');

    // 数据库中没有留下任何巡检记录
    const res = await fetch(`${BASE}/api/battens/G-01/inspections`);
    const db = await res.json();
    expect(db.inspections).toHaveLength(0);
  });

  it('同一吊杆同一营业日期重复提交时展示已完成提示', async () => {
    const user = await renderLoaded();

    // 第一次提交：三项全部正常
    await user.click(screen.getByRole('button', { name: '提交日检' }));
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('合格');

    // 紧接着对同一吊杆同一营业日期再次提交：服务端返回已完成提示
    await user.click(screen.getByRole('button', { name: '提交日检' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('已拒绝');
    expect(alert).toHaveTextContent('已完成');

    // 数据库中仍只有第一次的合格记录
    const res = await fetch(`${BASE}/api/battens/G-01/inspections`);
    const db = await res.json();
    expect(db.inspections).toHaveLength(1);
    expect(db.inspections[0].conclusion).toBe('PASS');
  });
});
