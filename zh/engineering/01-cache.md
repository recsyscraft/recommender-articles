# 推荐工程实践（1）：Cache 设计，支撑百毫秒级推荐的基石

全文章节结构如下，大家可以挑选自己感兴趣部分阅读：

1. 为什么要有 Cache ：开篇、概述。
2. Cache 的实现方案：对比 Redis 精确匹配和自研相似度匹配两种方案。
3. 自研 Cache 的设计：介绍相似度匹配原理（MinHash + LSH分桶）、实际工程设计（含伪代码）。
4. 实际案例：我实际参与的一个工程案例，以及当时的设计分析。
5. 总结

文中有部分公式的数学原理没有展开，大家如果感兴趣可以查阅相关资料，或者直接跳过公式部分，不影响阅读。

## 1. 为什么要有 Cache

推荐系统在线接口需要平均 100 毫秒返回，如果请求链路都经由业务模块实时计算，取画像/特征、召回、排序、重排，这些步骤叠加后很容易超时。所以在链路中需要增加缓存（Cache）模块，把短时间重复或者相似的请求由 Cache 模块承接。

> 注：本文中所述接口平均 100 毫秒返回只是示例，不同的系统对接口响应时间要求不完全一样，但基本都是百毫秒级别。

整个流程参考下图，该图首次出现在《推荐系统架构》的第10篇《[在线系统总览](../architecture/10-online.md)》中。下面流程描述有一些概念（例如 Recall Manager）也在这篇文章有介绍，如果之前没见过可以查阅。

![推荐系统在线流程|280](../../assets/architecture/10_engine_flow.png)

在该流程中，请求到达服务端之后，会首先尝试取缓存，此缓存即最终结果缓存。在推荐系统中，多个节点可以设置缓存，包括：

**结果缓存**

Key：场景、用户和计算配置指纹；Value：推荐批次、候选列表及生成来源元数据。这类缓存一般部署在整条链路的最末端，适合用户高频刷新的场景。（上图中的 Cache Hit 就是指是否命中结果缓存）

**召回缓存**

Key：场景、召回配置/索引版本、必要上下文和兴趣表示；Value：尚未应用个人过滤的可共享召回候选。这一类缓存一般在召回（Recall）阶段，由 Recall Manager 管理，可以缓解召回源的高频查询压力。

除了上面说到两类，其他的还可能有：

- 排序缓存。Key：用户特征列表加上待打分的 Content ID 集合；Value：Content ID 集合和对应的打分分数。这一类的缓存主要是为了解决同一批内容对同一用户的重复打分问题。

- Feature 缓存。把热门的 User Feature、Content Feature 缓存起来加速读取，Key 为 User ID 或者 Content ID。这部分一般在 Feature Server 内部实现。

本文为了简化，挑选**结果缓存**和**召回缓存**为例，讲解具体的工程实现方案。其他部分的缓存实现方案和这两类大同小异。

## 2. Cache 的实现方案

对于结果缓存，召回缓存，最基础通用的方案是采用 Redis，开发成本低，适用于大部分的场景。另外一种是自研 Cache 方案，优点是命中率高、业务定制化能力强，可以适配很多兴趣变化场景。在业界工程化中，两种方案一般都是配套使用。
### 2.1 通用方案：Redis 缓存

Redis 是业界最成熟的缓存体系之一，在推荐系统中广泛被使用。结果缓存、召回缓存，都可以直接用 Redis 来实现，是中小团队实现缓存的最快落地方案。

结果缓存是直接面向用户的，为了解决用户短时间重复刷新问题设立。所以结果缓存的 Key 一般设计带上用户 ID 做标识，形如：`result:{scene_id}:{uid}:{config_fingerprint}:{generation_id}`，config_fingerprint 覆盖影响结果的实验运行/服务配置、模型及策略版本和必要上下文；generation_id 标识不可变的推荐批次，最新批次另用指针定位。不是把所有字段机械塞进 Key，而是先定义哪些输入变化必须判为不兼容。Value 除候选内容 ID 列表外，还保存原特征快照引用、来源请求、模型/配置版本及生成时间，可以采用 JSON 或者 Protobuf 等序列化方式存储。中小规模场景用 JSON 会更方便调试；大规模下用 Protobuf，相对 JSON 可能减少字段名等开销，实际压缩比例和编解码成本应按具体 Schema 与载荷测量。结果缓存一般会设置 5 ~ 10 分钟的过期时间。过期时间设置太短会让缓存命中率下降，性能优化效果不佳；过期时间太长不能响应用户实时兴趣，降低体验。

召回缓存是为了解决兴趣标签、向量检索的重复计算和查询压力，Key 一般包含用户兴趣/向量 Hash 之后的值，例如：`recall:{scene_id}:{recall_fingerprint}:{context_key}:{interest_hash}`。recall_fingerprint 包含召回策略、模型/索引和兴趣表示版本；context_key 包括影响候选范围的地域、语言等上下文。共享 Value 是个人历史、权限和负反馈过滤之前的候选列表，不能带入原用户专属的过滤结果。召回缓存因为 Key 是用户兴趣，过期时间可以设置比结果缓存长一些，一般设置在 10 ~ 15 分钟。

如果采用 Redis 做缓存，可以在推荐引擎中直接添加相应代码来读写。也可以把 Redis 做个简单的封装，把 Key、Value 组装这些逻辑集中起来，推荐引擎直接调用封装之后的服务接口就行。这两种方式 Key 都是精确匹配；在召回缓存场景中，如果兴趣标签有任意微小变化，Key 都不一样，从而导致缓存不命中，达不到性能优化目的。

### 2.2 自研 Cache 方案

自研 Cache 相对于通用 Redis 方案，最大的改进在于 Key 支持相似度匹配，同时也可以在存储序列化、淘汰策略、读写性能上做定制化优化。

首先自研 Cache 存储可以有本地缓存和分布式两层，本地缓存采用内存结构，分布式可以采用已有的 Redis 等方案。对于本地缓存可定制化各类淘汰和加载策略，例如频繁访问的热点内容（非常高频使用的高频用户的结果缓存、新用户冷启动的召回缓存等），存放在本地实现更好的加速。其次在 Value 的序列化上可以采用更为定制化的二进制，比通用的 Protobuf 更节省存储空间。

另一项也是最核心的改进在于支持相似度匹配，适配推荐场景中，相似兴趣的推荐结果也类似的情况。大概的做法就是通过算法把用户高维兴趣特征转换为可比对的缓存标识，然后通过相似度算法来判断两个 Key 是否类似。这样就算用户实时兴趣有了小变化，通过相似度匹配也能命中缓存；此外对于一些具有长尾兴趣的用户，在一定相似度阈值内也能利用缓存加速。

在业界大的推荐平台中，很多系统都会采用这样的自研 Cache 方案，同时支持精确 Key 匹配和相似度匹配。这样既能支持热点用户结果缓存的精确型匹配，但仍需验证结果新鲜度和业务指标；也能支持相似用户、新用户冷启动用户、具有长尾兴趣的用户，通过相似匹配来优化系统整体性能。

## 3. 自研 Cache 的设计

推荐系统和数据库最大的区别在于：推荐结果不存在唯一正确答案。兴趣高度相似的用户可能共享部分召回候选。因此，下面的相似匹配仅用于同一兼容命名空间内的召回候选复用，不用于跨用户共享最终排序结果。标签相似也不代表权重、短期意图或权限相同；命中后仍要按当前用户过滤、补召回、取特征和排序。

自研 Cache 的核心点是通过用户兴趣的相似度来判断是否命中，下面我们来看看这个核心功能的大概原理和工程实现。
### 3.1 相似度匹配

用户兴趣可以看成一个标签集合，例如：{科技, 娱乐, 体育}（本例忽略权重，只保留标签是否存在）。两个用户兴趣集合的相似度，可以通过 Jaccard 相似度来计算，公式是
$$
J(A,B)=\frac{|A \bigcap B|}{|A \bigcup B|}
$$

也即是同时出现在两个集合中的元素个数和两个集合并集元素个数比值。例如，假设 A = {a, b, c, d, e}, B = {c, e, f, h}，Jaccard 相似度计算：
$$
J(A,B) = \frac{|A \bigcap B|}{|A \bigcup B|} = \frac{|\{c,e\}|}{|\{a, b, c, d, e, f, h\}|} = \frac{2}{7}
$$
所以这两个集合相似度可以表示 2/7 ≈ 0.2857。根据公式，Jaccard 相似度的值域为 0 ~ 1。

本例使用普通集合 Jaccard。若要比较非负兴趣权重，应定义加权 Jaccard 并使用相应的 [Weighted MinHash](https://ekzhu.com/datasketch/weightedminhash.html)；不能把普通 MinHash 直接解释成带权相似度。稠密 Embedding 则应选用适配其距离的检索方法。面对大量集合比较，可用 MinHash 签名估计相似度，降低重复比较成本。

### 3.2 MinHash 算法

理想 MinHash 对元素全集使用均匀随机排列，取每个集合中排列次序最小的元素。两个集合最小元素相同的概率等于它们的 Jaccard 相似度。用 k 个独立排列生成签名，比较**对应位置**相等的个数 y，估计值为 y/k；理想假设下无偏，方差为 s(1-s)/k。不能把两个签名的数值当作集合，再计算交并比。

工程上通常用哈希近似排列。下面以 k=128 为例，用固定的多组种子/盐值生成签名；不同盐值不严格保证独立或无偏，需检查哈希碰撞、估计误差及阈值附近的误命中。所有节点必须固定标签规范化、字符编码、哈希实现、种子和签名版本，升级时隔离索引。可参考 [MinHash 实现文档](https://ekzhu.com/datasketch/minhash.html)。

签名生成成本为 O(k×|标签集合|)，比较两个签名为 O(k)。若逐条扫描 N 条缓存，需 O(Nk)，因此还需要 LSH 索引减少候选。

### 3.3 LSH 分桶

LSH（Locality Sensitive Hashing，局部敏感哈希）把 k 维签名拆为 b 个 Band，每个 Band 含 r 个位置，k=b×r。查询 b 个 Band Key，将命中的记录 ID 去重，得到 U 条候选，再做完整签名比较。若每桶最多 m 条记录，读取至多 b×m 个引用，比较成本为 O(Uk)，还需计入 O(k×|标签集合|) 的签名生成、O(k) 的分桶和数据读取成本。b=16 是 Band 数量，不是签名维度 k=128。热点桶可能退化到 U≈N，应限制桶大小和总候选量；限额会带来漏检，需测量。

Band 的数量划分一般要考虑 Band 内部 MinHash 签名的个数。如果单 Band 内 MinHash 签名数量过多，可能存在微小变化从而导致 Band Key 变化的问题，最后导致漏命中、缓存命中率下降。如果单 Band 内 MinHash 签名数量过少，那样 MinHash 签名更容易相似，从而导致一个 Band 内列表过长，也即是前面所说的 m 值过大，计算过程变得更复杂。对于 k = 128 维的 MinHash 向量，可以选取 16 个 Band，每个 Band 内 8 个 MinHash 签名。

如果 b 表示 Band 数量，r 表示一个 Band 内部 MinHash 签名的数量，s 表示两个集合的 Jaccard 相似度。那么这两个向量至少有一个 Band 相同的概率为：
$$
P = 1-(1-s^r) ^b
$$
该公式假设签名位置及 Band 满足相应独立性，且不计额外的 Band Key 哈希碰撞。当 b=16、r=8、s=0.9 时，P≈99.9877%；s=0.8 时，P≈94.7049%。这是“进入候选集合”的概率，不是最终缓存命中率，也不是一定检索到。最终命中还受签名估计误差、阈值、TTL、配置兼容和候选限额影响；不存在通用的“95% 黄金拐点”，参数要用业务样本选择。

### 3.4 Cache 流程

根据前面的 MinHash 和 LSH 分桶原理讲解，可以设计以下结构。Band 索引和业务数据必须使用同一命名空间，包含场景、召回策略/索引版本和必要上下文；不允许先跨实验匹配，再仅凭相似度返回数据。

- Band Key 索引：Key 为带命名空间和 Band 编号的粗筛键，Value 为紧凑 entry_id 集合及成员到期时间；每条记录最多出现在 b 个桶中，不在每个桶复制完整签名。
- 业务数据缓存：Key 为命名空间内唯一 entry_id，Value 保存一次完整签名、候选列表、版本与到期时间。entry_id 不能直接用 32 位 Band 哈希充当。

请求先生成签名和 Band Key，批量读取索引、去重并限制候选数，再批量读取仍有效且版本兼容的业务记录。按对应位置相同比例估计相似度，选择超过阈值的最佳有效记录；某条数据已过期时应继续比较其他记录。空兴趣集合跳过相似缓存，进入明确的冷启动召回。

未命中时计算可共享候选，先写主数据，再幂等写入各桶的 entry_id 和到期时间。索引写入失败只影响可发现性；主数据过期后的死引用需惰性删除和定期清理，空桶回收，重复写入不能无限追加列表。仅刷新整个桶的 TTL 会让老成员长期残留，不能替代成员级过期。

候选返回后，仍按当前用户的历史、屏蔽、权限过滤，必要时补候选，再取特征和排序。原用户专属分数不能当作当前用户分数；需重算、标记不可复用，或只复用 ID。

### 3.5 核心伪代码

以下为接口语义伪代码，省略缓存读失败兜底、RPC Deadline、并发控制和清理任务。缓存读取失败应按未命中处理；所有操作受请求预算约束。函数仅返回共享召回候选；scope 同时约束索引与数据缓存。调用方还需执行个人过滤和排序，不能把返回值直接当作对用户的完整推荐响应。


```java

private static final double SIMILARITY_THRESHOLD = 0.9;
private static final int MINHASH_SIGN_LEN = 128;
private static final int LSH_BAND_COUNT = 16;
private static final int LSH_UNIT_PER_BAND = MINHASH_SIGN_LEN / LSH_BAND_COUNT;
private static final int CACHE_TTL_SECONDS = 600;

public List<Long> querySharedRecallWithLshCache(RecallScope scope, Set<String> userInterest) {
    // scope 绑定召回/索引/上下文、标签规范化及哈希版本
    if (userInterest.isEmpty()) {
        return recallSharedCandidates(scope, userInterest);
    }
    long[] currentSign = buildUserMinHashSign(userInterest);
    Set<String> bandKeys = buildLshBandKeys(currentSign);

    // 有界查询：逐桶过滤过期成员、限制返回量，再全局去重和限额
    Set<Long> entryIds = cacheCluster.queryLiveEntryIdsBounded(scope, bandKeys);
    List<CacheEntity> candidates = cacheCluster.batchGetData(scope, entryIds);
    CacheEntity best = null;
    double bestSimilarity = -1.0;
    for (CacheEntity entity : candidates) {
        // 主数据可能已淘汰；版本/到期条件以主记录为准
        if (entity == null || !entity.isCompatibleAndLive(scope)) continue;
        double similarity = estimateJaccard(currentSign, entity.getMinHashSign());
        if (similarity >= SIMILARITY_THRESHOLD && similarity > bestSimilarity) {
            bestSimilarity = similarity;
            best = entity;
        }
    }
    if (best != null && best.isCompatibleAndLive(scope)) {
        return best.getItemIdList();
    }

    List<Long> itemList = recallSharedCandidates(scope, userInterest);
    // 缓存失败按未命中处理，仍返回召回结果；省略具体超时与错误监控
    try {
        long entryId = cacheCluster.allocateUniqueEntryId(scope);
        CacheEntity entity = new CacheEntity(currentSign, itemList, scope,
                                            nowEpochSeconds() + CACHE_TTL_SECONDS);
        cacheCluster.putData(scope, entryId, entity, CACHE_TTL_SECONDS);
        for (String bandKey : bandKeys) {
            cacheCluster.upsertIndexMember(scope, bandKey, entryId, entity.getExpiresAt());
        }
    } catch (CacheException e) {
        recordCacheWriteFailure(e); // 后台清理孤立记录和死引用
    }
    return itemList; // 调用方继续个人过滤、补候选和排序
}

// 比较签名的对应位置，不是对签名数值集合求交并比
public double estimateJaccard(long[] a, long[] b) {
    if (a.length != MINHASH_SIGN_LEN || b.length != MINHASH_SIGN_LEN) {
        throw new IllegalArgumentException("incompatible signature length");
    }
    int equal = 0;
    for (int i = 0; i < a.length; i++) {
        if (a[i] == b[i]) equal++;
    }
    return (double) equal / a.length;
}



/**
 * 生成 MinHash 向量
 */
public long[] buildUserMinHashSign(Set<String> interestTags) {
    long[] minHashSign = new long[MINHASH_SIGN_LEN];
    Arrays.fill(minHashSign, Long.MAX_VALUE);
    // 多组固定盐值近似独立排列；固定 UTF-8/哈希版本并离线验证误差
    for (String tag : interestTags) {
        for (int idx = 0; idx < MINHASH_SIGN_LEN; idx++) {
            long currHash = murmurHash(tag + "_salt_" + idx);
            if (currHash < minHashSign[idx]) {
                minHashSign[idx] = currHash;
            }
        }
    }
    return minHashSign;
}

/**
 * LSH分桶：根据MinHash签名计算归属桶Key
 */
public Set<String> buildLshBandKeys(long[] minHashSign) {
    Set<String> bandKeySet = new HashSet<>(LSH_BAND_COUNT);
    for (int bandIdx = 0; bandIdx < LSH_BAND_COUNT; bandIdx++) {
        int start = bandIdx * LSH_UNIT_PER_BAND;
        int end = start + LSH_UNIT_PER_BAND;
        long[] bandSubSign = Arrays.copyOfRange(minHashSign, start, end);
        // 32 位哈希可能碰撞，只作粗筛；后续完整签名复核，不能作 entry_id
        String bandKey = LSH_BUCKET_INDEX_PREFIX + bandIdx + "_" + Arrays.hashCode(bandSubSign);
        bandKeySet.add(bandKey);
    }
    return bandKeySet;
}

```

## 4. 实际案例

以下是我之前经历过的一个实际案例相关数据，以结果缓存和召回列表缓存为例。

提前说明一下，以下案例来自我参与过的实际推荐系统项目。文中的系统架构、设计思路、上线结果都来自真实工程实践。但由于时间较久，部分原始测试记录已经缺失，文中少量容量估算、用户规模等数据根据当时保存的结果进行了反推和整理，因此下面把历史观测与按明确假设复算的容量分开；未保存的统计口径无法补证，不能将估算当作已验证的生产容量或普适收益。

### 4.1 两级缓存方案

系统设置两级缓存，第一级为 L1 缓存，保存用户最终排序结果；第二级 L2 缓存，保存召回阶段列表。

- L1 缓存，Key 为场景、User ID、配置指纹和 generation_id；Value 为生成的 200 个 Item ID、打分及来源元数据。批次是待分页的候选，不是已经通过本次出口检查的响应。
- L2 缓存，在兼容的召回命名空间内，主数据 Key 为唯一 entry_id，Value 保存一份 MinHash 签名、约 1000 条候选、版本和到期时间；候选包含 Item ID、来源及可复用的源内分数。Band 索引保存 entry_id 和成员到期时间，不重复保存完整签名。

整体流程大概为：

1. 请求先固定实验/服务配置快照，再按用户、场景和配置指纹查询 L1。翻页 cursor 绑定 generation_id、下一候选偏移和页面策略版本；命中后跳过昂贵的召回精排，但仍执行本页组装、广告占位、最终有效性校验和 Delivery Log。
2. L1 未命中时，获取用户画像并进入 Recall Manager；在兼容命名空间内查 L2，未命中则计算可共享候选并写入 L2。
3. 无论 L2 是否命中，都按当前用户的历史、权限、屏蔽和负反馈过滤，重新获取个性化特征并排序。原用户专属分数不复用为当前用户打分。
4. 重排产生批次并记录原始 Feature Log，将特征引用、source_request_id、generation_id 和模型/实验配置版本随候选写入 L1，然后再组装本页。

**分页和重试契约**：翻页继续同一批次，下拉刷新明确创建新批次，不能把两者都当作“把共享 offset 加 20”。同一 cursor 的并发请求通过原子创建页面映射或幂等键得到同一页候选与下一 cursor，不重复推进消费进度。过期、配置不兼容或越界时显式返回重启分页标记，不能把旧偏移套到新批次；旧批次可以按保留策略供在途翻页使用。幂等记录需有有效期和用户绑定。

**所有出口共用最终校验**：在广告、运营插入和截页完成后检查最新下线、用户屏蔽、权限、重复以及本页位置/配额约束。过滤后从后续候选或兜底中有限补齐，新补内容同样检查；无法确认安全则少返回或空返回，不能因为 TTL 未到期就放行。重试沿用页面映射，但仍应用新的撤销/屏蔽；因此幂等保证不重复消耗候选，不承诺安全状态变化后字节完全相同。最终校验之后不再随意改动展示集合或位置。

**日志闭环**：Feature Log 记录生成时的特征和批次内位置；每次返回另记 Delivery Log，包含当前 request_id/trace_id、实际下发 item、最终位置、缓存来源和原特征引用。客户端有效展示后才生成 impression_id，并与下发条目关联。尚未展示的 200 条候选不能标为 CTR 负例；缓存条目特征引用缺失时显式标记，不用当前特征冒充原快照。

约束应注明作用域：整批、当前页、会话或跨页窗口，以及是否计入广告。例如 200 条中视频占比 10% 不代表每 20 条页也满足 10%。跨页多样性应读取会话历史，并通过版本或串行化推进页状态，避免两个并发页互相看不到对方。缓存主体大小与这些元数据、分页映射和日志的额外成本需要分别估算；后文历史容量数字只描述当时的候选载荷口径。

### 4.2 离线预估

#### 4.2.1 L1 用户结果缓存

**TTL 和命中率** 

离线用过往数据进行实际测评，得到缓存 TTL 和命中率关系：

| TTL    | Hit Rate |
| ------ | -------- |
| 60 s   | 0.11     |
| 300 s  | 0.28     |
| 600 s  | 0.36     |
| 1200 s | 0.44     |

在设计时一般采用 TTL 300 秒（5 分钟），兼顾系统效率和用户体验。TTL 300 秒时，命中率大概为 0.28。

**容量估算**

本例每条 L1 记录保存 200 个 Item。仅当 ID 范围在 [0, 2³²−1] 时才可用 uint32（4 B）；超出时需 uint64 或可逆字典映射，并计入映射成本。

若分数已明确归一化到 [0,1] 且量化误差可接受，可存为 uint16（2 B）：

```text
s = clamp(score, 0, 1)             // 先检查有限值；非有限值拒绝或按协议兜底
q = round(s × 65535)               // 0..65535，score=1 不溢出
restored_score = q / 65535
```

量化步长为 1/65535≈0.00001526，四舍五入最大绝对误差约为半个步长（不含浮点实现误差）。量化可能产生并列分数，应保留稳定顺序或明确 tie-break，并验证 Top K 及融合效果。任意排序分、内积和热度分不一定在 [0,1]，不能直接套用；未验证归一化时可保存 float32 等格式。

在上述前提下，候选裸载荷为 200×(4+2)=1200 B。仅为演示，假设旧格式连同 Key 等占 1280 B（1.25 KiB）/条；这不是 Redis 通用开销，完整版本、特征引用、分页和索引元数据必须另行测量。本文统一采用 1 KiB=1024 B、1 GiB=2³⁰ B。

历史场景按 5 分钟内高峰活跃 100 万用户、突发翻倍至 200 万条预留，则 1280×200万/2³⁰≈2.384 GiB；再假设碎片与运行缓冲系数 1.25，约为 2.980 GiB。该数只演示单份主数据预算，实际记录数还受多场景、配置版本和保留批次影响。副本需分别计入，不能用 25% 余量代替一整份副本；最终容量应以目标格式和部署参数下的内存测量、峰值压测为准。

#### 4.2.2 L2 召回列表缓存

**相似度和命中率**

L2 缓存采用兴趣相似来命中，下面保留历史数据（命中率分母缺失，见 §4.3），取 TTL = 10 分钟时，得到相似度阈值和命中率关系如下（其他 TTL 数据丢失，只找到上线实验时的这份数据）：

| Sim. Threshold | Hit Rate |
| -------------- | -------- |
| Exact Match    | 0.31     |
| 0.9            | 0.38     |
| 0.8            | 0.40     |
| 0.7            | 0.41     |

新用户或者轻度使用的用户，很容易具备相同的用户画像，因此精确匹配时也能有 31% 的命中率。最后和业务团队一起确定，取相似度 0.9 作为线上实验阈值。

**容量估算**

假设每条记录含 1000 个候选，每个候选为 item_id 4 B、单一来源编号 1 B、归一化量化分数 2 B，则裸候选载荷为 7000 B。适用条件与 L1 相同：ID 不能溢出，源编号不超过编码范围，分数可量化。多路来源需列表/位图及各源分数，使用 float32 则每个分数多 2 B；这些都要重新计费，不能静默丢掉多来源契约。

沿用历史反推假设：10 分钟峰值 400 万用户，60% 的用户平均 3.5 人共享一条、30% 平均 1.8 人共享一条、10% 不共享：

```text
N = 400万 × 60% / 3.5 + 400万 × 30% / 1.8 + 400万 × 10%
  ≈ 1,752,381 条
```

这只是旧场景的条目数估算。实验隔离、多个场景或配置版本会降低共享率；持续未命中写入、重复记录与淘汰策略也会改变 N，不能只用活跃用户数推定实际在存条目数。

按第 3.4 节“主表一份签名、索引仅存 ID”的设计，裸数据下界如下：

| 项目 | 每条主记录对应字节数 | N 条合计 |
| --- | ---: | ---: |
| 候选载荷 | 7000 B | 11.424 GiB |
| 128 维 uint64 签名，仅存一次 | 1024 B | 1.671 GiB |
| 主记录 entry_id | 8 B | 0.013 GiB |
| 16 个 Band 成员 ID | 16×8=128 B | 0.209 GiB |
| 16 个成员到期时间（按 8 B 计） | 16×8=128 B | 0.209 GiB |
| 合计 | 8288 B | 13.526 GiB |

该下界未含命名空间、Band Key、版本/主记录到期字段、容器和 Redis 对象开销、碎片、过期清理滞后、临时查询缓冲及副本。需测量“主表实际占用 + 索引实际占用”，再分别预留运行缓冲和副本容量，不能用统一 1.3 倍系数隐含所有成本。若每个 Band 复制一份 1024 B 签名，仅索引签名就需 N×16×1024≈26.739 GiB，原先约 18.1 GB 的总预算无法覆盖这种结构。

### 4.3 实际上线测试情况

下面保留历史观测，耗时都是均值，不能替代 P95/P99 或请求 Deadline。原始记录不完整，无法确认计时是否包含 Gateway/网络、不同阶段是否采用相同流量和负载，因而不把这些数字当成严格的同口径性能实验。

**L1 结果缓存上线**

历史记录中点击率、停留时长、留存未见明显变化，L1 命中率约 31%，内存最高记录不超过 2.5 GB（原单位与测量范围未留存）。请求均值从 198 ms 降至 141 ms，按这两个数复算降幅为 **28.79%**。这不构成完整元数据格式下的容量保证，也不能在缺少区间估计时证明业务效果完全等价。

**L2 召回缓存上线**

历史记录为 CTR 相对下降约 3%，阅读时长和留存未见明显变化。若原 CTR 为 c，则新值约为 0.97c，绝对下降为 0.03c；缺少原 CTR、实验周期、样本量和置信区间，无法补出百分点变化或判断显著性，不能用“小数点后第三位”认定损失可忽略。上线取舍应依据预定业务护栏和成本收益重新验证。

L2 命中率历史记录约 38%，但现存材料无法确认分母是全部入口请求，还是 L1 未命中后到达 L2 的请求；前面的阈值表也存在此口径缺失，不能据此精确分解总体收益。重测时应分别记录 L2 条件命中率和入口贡献占比。

召回阶段均值从 98 ms 降至 68 ms，复算下降 **30.61%**；整体均值从 198 ms 降至 108 ms，复算下降 **45.45%**。若假设 L1 命中率始终为 31%，仅余下 69% 的请求在召回上节省 30 ms，整体均值应由 141 ms 降至约 120.3 ms，而非直接得到 108 ms。额外差异可能来自排队、负载或统计范围变化，现存记录不足以归因。

按 98/68−1 可算出约 44.12%，但只有固定并发、稳定负载等条件下，延迟倒数才能用于相应吞吐关系；它不是最大吞吐必然提升的比例。历史压测记录的单机吞吐提升约 20%。新增缓存访问、序列化和签名计算会增加 CPU 成本，是当时分析的可能瓶颈；应以相同硬件、负载和尾延迟约束下的压测来验证最大吞吐。

## 5. 总结

本文主要介绍了缓存在推荐系统链路中的作用，以及两种缓存的工程落地方案。简要的总结以下几点：

1. 缓存分层设计，逐级优化链路响应时间。
2. 两类不同的缓存，精准满足不同阶段的业务和性能要求。
3. 缓存的参数选择需要在性能和推荐效果之间权衡。
4. 按需求、收益和维护成本选择 Redis 封装或自研，团队规模不是唯一依据。
5. 缓存优化带来的响应时间下降，并不一定等比例转化为吞吐量提升。

整体而言，缓存是推荐系统（也是其他系统）的最基础，最具性价比的优化策略之一。分析好链路中的业务场景，不同场景使用不同优化方案，能达到性能和推荐效果的平衡。


