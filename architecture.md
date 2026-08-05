# Kiến trúc hệ thống Multi-Agent — K4 Day 9

Hệ thống điều tra 50 khiếu nại thương mại điện tử trên dữ liệu Olist bằng 8 agent
giao tiếp qua một message bus A2A. Tài liệu này mô tả sơ đồ agent, vai trò, quyền
truy cập dữ liệu và luồng handoff.

- Model: `gpt-4o-mini` (khai báo tại `src/config.py`, ghi lại trong `logging/metadata.json`)
- Orchestration: message bus tự viết (`src/a2a/`), không phụ thuộc agent framework
- Entry point: `python run.py`

## 1. Sơ đồ agent

```
                          input/EC_xxx.json
                                 │
                                 v
                    ┌────────────────────────┐
                    │      COORDINATOR       │  không sở hữu scope dữ liệu nào
                    │   (src/pipeline.py)    │  chỉ định tuyến + gộp fact
                    └────────────┬───────────┘
                                 │
          ┌──────────────┬───────┴────────┬──────────────────┐
          v              v                v                  v
   ┌────────────┐ ┌────────────┐  ┌──────────────┐   ┌──────────────┐
   │  intake    │ │  customer  │  │ order_product│   │   delivery   │
   │  (LLM)     │ │            │  │              │   │              │
   └────────────┘ └────────────┘  └──────┬───────┘   └──────────────┘
                                          │ item_sum_raw, freight_sum_raw
                                          v
                                   ┌──────────────┐
                                   │   payment    │
                                   └──────┬───────┘
                                          │
                     tất cả fact ─────────┴──────────┐
                                                     v
                                          ┌────────────────────┐
                                          │      policy        │
                                          │ rule engine (chốt) │
                                          │ + LLM adjudicator  │
                                          └─────────┬──────────┘
                                                    v
                                             assemble draft
                                                    v
                                          ┌────────────────────┐
                                          │  critic (LLM)      │ observer,
                                          │  chỉ ghi trace     │ không sửa field
                                          └─────────┬──────────┘
                                                    v
                                          ┌────────────────────┐
                                          │     verifier       │◄─┐
                                          │  (deterministic)   │  │ repair
                                          └─────────┬──────────┘  │ tối đa 2 vòng
                                                    │─────────────┘
                                                    v
                                          output/EC_xxx.json
```

## 2. Vai trò và quyền truy cập dữ liệu

Quyền truy cập **được cưỡng chế lúc chạy**, không phải quy ước. Mỗi agent khai báo
`capabilities`; bus dựng một `DataView` giới hạn đúng các bảng đó (`src/store/views.py`).
Agent chạm vào bảng chưa khai báo sẽ nhận `ScopeError` ngay lập tức.

| Agent | Vai trò | Quyền truy cập CSV | LLM |
| --- | --- | --- | :-: |
| `coordinator` | Điều phối, gộp fact sheet, chạy repair loop | **không có** | – |
| `intake_agent` | Đọc khiếu nại tiếng Việt → claim có cấu trúc | **không có** (chỉ đọc file input) | ✓ |
| `customer_agent` | `customer_unique_id` và lịch sử order | `orders`, `customers` | – |
| `order_product_agent` | Trạng thái đơn, item, seller, product, category | `orders`, `items`, `products` | – |
| `payment_agent` | Gộp payment row, đối soát với item + freight | `payments` | – |
| `delivery_agent` | Delivery variance, seller handoff variance | `orders`, `items` | – |
| `policy_agent` | Áp `EC_POLICY_V2` + ý kiến thứ hai độc lập | **không có** (chỉ dùng fact được handoff) | ✓ |
| `critic_agent` | Phản biện draft (advisory) | **không có** | ✓ |
| `verifier_agent` | Schema, array cap, null, evidence ID | `orders`, `items`, `payments`, `sellers` | – |

Hai điểm đáng chú ý:

- `payment_agent` **không có** scope `items`. Muốn đối soát, nó buộc phải nhận tổng
  item và freight từ `order_product_agent` qua envelope. Đây là handoff thật, không
  phải hai agent cùng đọc chung một biến global.
- `policy_agent` và `critic_agent` không có scope dữ liệu nào. Chúng chỉ suy luận
  trên fact đã được các agent khác xác minh, nên không thể tự bịa ra sự kiện.

## 3. Luồng handoff của một case

| # | Từ → Đến | Intent | Nội dung bàn giao |
| -: | --- | --- | --- |
| 0 | coordinator → verifier | `validate_request` | case gốc — **chặn tại cửa nếu input không hợp lệ** |
| 1 | coordinator → intake | `parse_claim` | `customer_request`, `investigation_scope` |
| 2 | coordinator → customer | `resolve_identity` | `order_id` |
| 3 | coordinator → order_product | `inspect_order` | `order_id` |
| 4 | coordinator → delivery | `analyse_delivery` | `order_id` |
| 5 | coordinator → payment | `reconcile` | `order_id` + **tổng item/freight từ bước 3** |
| 6 | coordinator → policy | `apply_policy` | fact sheet đã gộp |
| 7 | coordinator → critic | `challenge_draft` | fact sheet + draft |
| 8 | coordinator → verifier | `validate` | draft + `order_id` |
| 9 | (nếu lỗi) repair → verifier | `validate` | draft đã chuẩn hoá |

Mỗi lượt đi và về đều sinh một dòng trong `logging/trace.jsonl`. Run gần nhất:
**1088 dòng** cho 50 case — 400 `handoff`, 400 `report`, 150 `llm_call`, 50
`case_start`/`case_end`.

## 4. Ranh giới LLM — nguyên tắc thiết kế cốt lõi

85% rubric là số học trên CSV. Model 8B-class không phải công cụ đúng để làm số học,
nên ranh giới được đặt như sau:

| Do code deterministic quyết định | Do LLM tác động |
| --- | --- |
| primary/secondary issue, root cause, responsible party | *(không field nào)* |
| toàn bộ số tiền, số giờ, reconciled | |
| affected entities, context, evidence ID | |
| resolution actions, case_status | |
| **`confidence`** ← chỉ field này chịu ảnh hưởng, qua mức đồng thuận của adjudicator | ✓ |

`confidence` là field duy nhất README không định nghĩa, nên nó là nơi duy nhất an
toàn để phản ánh mức đồng thuận giữa các agent. Mọi giá trị khách quan còn lại nằm
trong `src/policy/rules.py`.

## 5. Cơ chế kiểm chứng

### 5.0. Gate đầu vào (chạy trước mọi agent)

`verifier_agent` gác **cả hai chiều**. Trước khi bất kỳ agent nào chạm vào case,
`schema.validate_request` kiểm:

| Kiểm tra | Chặn khi |
| --- | --- |
| `case_id` | lệch tên file |
| `policy_version` | khác `EC_POLICY_V2` — pipeline không cài rule set nào khác |
| `claimed_order_id` | thiếu, sai định dạng 32 ký tự hex, hoặc **không tồn tại trong orders.csv** |
| `customer_request` | thiếu hoặc không phải object |
| `investigation_scope.*` | cờ không phải boolean |

Case bị chặn **không sinh file output**, được ghi event `request_rejected`, đếm vào
`metadata.json.runtime.requests_rejected` và làm run trả exit code 2.

Lý do có lớp này: nếu chỉ gác output, một request hỏng vẫn chảy hết pipeline rồi ra
một document trông rất tự tin. Chặn ở cửa là khác biệt giữa "không có câu trả lời" và
"một câu trả lời sai trông như đúng".

### 5.0.b `investigation_scope` là chỉ thị, không phải trang trí

`include_customer_history: false` → `related_order_ids` rỗng **và** không gắn
`repeat_customer`. `include_product_context: false` → `product_ids`/`category_names`
rỗng **và** không gắn `multiple_categories`. Áp dụng trên fact sheet trước khi policy
chạy, để việc ta không được phép điều tra thì cũng không được phép kết luận.

### Ba lớp kiểm chứng đầu ra

1. **LLM adjudicator** (`policy_agent`) — phân loại lại case từ fact sheet, hoàn toàn
   độc lập với rule engine. Bất đồng được ghi trace và trừ điểm `confidence`.
   Run cuối: **0/50 bất đồng**. Trong quá trình phát triển, tín hiệu này bắt được 18
   bất đồng, mỗi cái đều truy ra một lỗi prompt thật (xem `docs/design_notes.md`).
2. **Verifier** (deterministic) — dựng lại tập ID hợp lệ từ CSV chứ không tin draft,
   rồi kiểm schema, array cap, null handling, evidence ID. Draft trượt sẽ được
   chuẩn hoá và kiểm lại, tối đa 2 vòng. Run cuối: **0 vi phạm, 0 case cần repair**.
3. **Audit ngoài pipeline** (`tools/audit.py`) — không import `src/`, tự đọc lại CSV
   bằng stdlib và tính lại mọi giá trị được chấm, rồi diff với `output/`. Run cuối:
   **PASS 50/50**.

`critic_agent` vẫn chạy trên mọi case và mọi phản biện đều vào trace, nhưng **không
sửa field nào** — kể cả `confidence`. Lý do đo được nêu trong `docs/design_notes.md`.

## 6. Xử lý lỗi

- Một agent ném exception → bus bắt, ghi `agent_error`, trả report `degraded`; case
  đó xuống cấp chứ không giết cả run.
- LLM lỗi/timeout → retry backoff 3 lần, sau đó trả `None`; mọi caller đều đã có sẵn
  đáp án deterministic nên output vẫn hợp lệ. Chạy `python run.py --no-llm` cho ra
  đúng 50 file với cùng mọi giá trị khách quan, chỉ khác `confidence`.
- Order không có item row (6 case `unavailable`) → `expected_total_brl`,
  `difference_brl`, `reconciled` là `null`; item/seller/product/category/handoff là
  mảng rỗng, đúng README mục 4.

## 7. Cấu trúc thư mục

```
run.py                      entry point
src/config.py               tên model, tham số decode, đường dẫn
src/store/olist_store.py    load + index 9 CSV (bỏ geolocation, reviews: không dùng)
src/store/views.py          cưỡng chế quyền truy cập theo capability
src/a2a/message.py          envelope A2A
src/a2a/bus.py              định tuyến + cưỡng chế scope + trace
src/a2a/trace.py            ghi trace.jsonl (truncate mỗi run)
src/llm/client.py           OpenAI JSON mode, deterministic, không bao giờ fatal
src/policy/rules.py         EC_POLICY_V2 dạng hàm thuần
src/policy/schema.py        schema + array cap + hard gate
src/agents/                 8 agent
src/assembler.py            dựng document + chuẩn hoá khi repair
src/pipeline.py             coordinator
tools/audit.py              audit độc lập (không nằm trong zip nộp bài)
```
