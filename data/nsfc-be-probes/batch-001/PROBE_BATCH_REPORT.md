# NSFC B/E批准号第一批文献证据探针报告

## 执行范围

- 输入候选批准号：200。
- B口：21；E口：179。
- 自动来源：Crossref、OpenAlex Awards、OpenAlex Works。
- 本轮不执行PI推定，不把OpenAlex候选题名、负责人或单位直接写入项目主表。

## 结果

- `confirmed_multi_channel`：0。
- `confirmed_openalex_award`：0。
- `confirmed_bibliographic`：87。
- `award_number_only`：3。
- `no_match_all_sources`：110。
- `inconclusive_source_error`：0。

- 文献/奖项证据行：445。
- 进入官方/机构页面升级队列的已确认候选：87。
- Web后续核查队列：200。

## 状态含义

- `confirmed_multi_channel`：至少两个API证据通道同时确认批准号与NSFC资助关系；OpenAlex可能聚合Crossref等来源，因此不自动视为完全独立证据。
- `confirmed_openalex_award`：OpenAlex Award实体确认批准号与NSFC，但仍需官方或依托单位页面升级。
- `confirmed_bibliographic`：论文资助元数据确认批准号与NSFC。
- `no_match_all_sources`：本次成功查询的自动来源均未命中；不得据此判定空号。
- `inconclusive_source_error`：至少一个来源请求失败，需重试后再判断。

## 后续

先对已确认候选进行基金委、高校或研究所页面升级；再对无文献命中的高优先级编号执行精确Web检索。编号存在性与负责人身份继续分开管理。
