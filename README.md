# 国家教材版本接续

把教材版次、内容单元、法规依据、学校采用与勘误决定组织成**带有效区间的接续链**：
所有状态由只增事件按实际发生时间回放得到，任意历史日期都能还原学校当时应当使用的文本。

## 领域规则

- **可区分的实物与事实**：版次、在印印刷批次、学校已采用的电子版（form=electronic/print）、
  教师备课引用与已发生的教学事实分别建模，不再只能找到版次和通知。
- **有效区间还原**：采用链与内容单元都带 `[effective_from, effective_to)` 区间，
  `resolve_text(school, course, unit_code, at)` 可还原任意历史日期的文本、当时法规依据与批次。
- **勘误分级处理**：
  - `safety`/`major`（安全性或教学影响重大）：先 `FREEZE_IMPOSED` 冻结受影响内容，
    学校须按 **课程 → 学段 → 本地补充材料** 三层分批确认（`FREEZE_ACK_LAYER`），
    每批只解除点名的单元与层级，全部解除前对应文本状态为 `frozen`，且禁止切换版次；
  - `normal`：排队进入**下个可用版次**，版次发布时收录（`CORRECTION_INCLUDED`）。
- **教学事实不可变**：`CONTENT_TAUGHT`、`PREP_REFERENCED` 一经记录永不修改；
  冻结、确认、出新版、切换都只追加事件，不触碰历史事实。
- **切换前核对**：`confirm_switch` 必须逐项核对课程、学段与本地补充材料哈希，
  且当前版次上未解除的冻结必须先确认完毕。
- **迟到入链**：问题按“实际发现时间”（`found_at`）作为事件时间入链，
  登记时刻另存 `recorded_at`，投影按 `occurred_at` 排序回放归位。
- **重复确认幂等**：同一通知内容一致的再次确认返回原决定事件；
  内容变化则追加 `DECISION_REVIEW_PENDING` 转待核，原决定继续有效、不被覆盖。
- **可注入时钟**：`SystemClock`/`FixedClock` 注入全部时间判断；
  逾期与升级提醒由事件中的原始截止点相对时钟现算，
  进程从 JSON 事件日志恢复后沿用同一截止点，不随停机漂移。
- **监管血缘**：`regulatory_trace(notice_id)` 从一条替换/勘误记录给出
  仍受阻学校清单、各校各单元确认到哪一层、生效选择所依据的法规与勘误事件版本、
  替换目标与待核记录。

## 模块

- `src/clock.py`：可注入时钟（`SystemClock` / `FixedClock`）。
- `src/event_store.py`：只增事件日志；事件 id 唯一、聚合 version 从 1 严格连续，
  支持 JSON 落盘与进程恢复。
- `src/textbook_service.py`：领域命令服务与有效区间投影（`Projection`）。
- `src/reminders.py`：逾期冻结提醒与新版升级提醒。
- `src/validator.py`：基础事件信封校验。
- `contracts/domain.schema.json`：聚合类型与事件名称约定。
- `tests/test_textbook_service.py`：端到端领域场景测试（16 个用例）。

## 本地检查

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

所有命令均可在单个 Linux 应用容器内执行，不需要外部服务。
