export const releaseViewTemplate = `
<section class="panel">
  <h2>解笼拨付</h2>
  <p class="hint">仅核验通过的笼可拨付。收款方只走笼面最新受益人一侧：写拨付前服务端重读，旧快照对不上即记「快照过期偏航」零增拒写。账户余额与冻结同步扣减。</p>
  <div class="grid-2">
    <div>
      <div class="form-row">
        <div>
          <label>通过态笼</label>
          <select v-model.number="releaseForm.cage_id" @change="loadReleaseCageFace">
            <option :value="0">请选择</option>
            <option v-for="c in passCages" :key="c.id" :value="c.id">
              {{ c.cage_code }} · 可用 {{ fmt(c.available) }}
            </option>
          </select>
        </div>
        <div>
          <label>金额（空=全额）</label>
          <input type="number" v-model.number="releaseForm.amount" min="0" step="1000" placeholder="全额"/>
        </div>
        <div>
          <label>收款方快照（空=笼面最新受益人）</label>
          <input v-model="releaseForm.payee" placeholder="留空以笼面为准"/>
        </div>
      </div>
      <p class="hint" v-if="releaseCageFace">
        笼面最新受益人：<b>{{ releaseCageFace.beneficiary }}</b>
        （拨付前重读；旧快照作废，对不上即拒付）
      </p>
      <div class="actions">
        <button class="ok" @click="doRelease">解笼拨付</button>
      </div>
    </div>
    <div>
      <table>
        <thead>
          <tr><th>凭证</th><th>笼</th><th>金额</th><th>收款方</th><th>名册</th><th>笼代</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in releases" :key="r.id">
            <td>{{ r.release_ref }}</td>
            <td>{{ r.cage_code }}</td>
            <td>{{ fmt(r.amount) }}</td>
            <td>{{ r.payee }}</td>
            <td>
              <span class="badge" :class="r.cage_beneficiary===r.payee?'pass':'fail'">
                {{ r.cage_beneficiary===r.payee ? '同侧' : '撕扯:'+r.cage_beneficiary }}
              </span>
            </td>
            <td>{{ r.cage_generation }}</td>
            <td>{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
