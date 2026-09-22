# 代码签名申请指引（可选，消除 SmartScreen 提示）

未签名的 exe 会触发 Windows SmartScreen「未知发布者」提示，也让部分杀软更敏感。
本项目是 MIT 开源，**可申请 SignPath Foundation 的免费 OV 代码签名**。

> 不签名也能用（首次点「仍要运行」即可）。本文只是「更专业」的增强项。

---

## 方案 A：SignPath Foundation（免费，推荐）

**资格**：OSI 认可的开源许可证（本项目 MIT ✅）、公开仓库、有实际用户。

**步骤**

1. 访问 https://signpath.org/ 提交开源项目申请（OSS program）。
2. 填写：仓库地址（`https://github.com/cewtoad/FI`）、许可证（MIT）、
   发布方式（GitHub Releases）。
3. 审批约 1–5 个工作日。批准后会分配：
   - SignPath Organization ID
   - 一个 **Project Slug** 和 **Signing Policy Slug**
4. 安装 SignPath 的 GitHub App 到仓库，配置 `.github/workflows/release.yml` 的
   签名步骤（SignPath 会给出可直接粘贴的 YAML 片段，通常形如）：

   ```yaml
   - name: Sign with SignPath
     uses: signpath/github-action-submit-signing-request@v1
     with:
       api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
       organization-id: ${{ secrets.SIGNPATH_ORG_ID }}
       project-slug: f1-race-engineer
       signing-policy-slug: release-signing
       artifact-configuration-slug: zip
       github-artifact-id: ${{ steps.upload.outputs.artifact-id }}
       wait-for-completion: true
       output-artifact-directory: signed
   ```

5. 把签名后的产物替换掉未签名的再上传 Release。

**注意**：SignPath 免费额度要求项目持续开源，且签名走他们的 CI（不持有你的私钥）。

---

## 方案 B：Azure Trusted Signing（付费，约 $10/月）

- 适合想完全自控流程的情况。
- 需要 Azure 订阅 + 企业/个体身份验证（组织验证）。
- 优点：签名即时、可信度高；缺点：有月费、验证周期长。

---

## 方案 C：过渡期不签名

发布页固定注明：

- 提供 `SHA256SUMS.txt` 校验值
- 提供 VirusTotal 扫描链接
- 说明 SmartScreen 弹窗处理：「更多信息 → 仍要运行」

对个人项目/小范围分发完全够用；签名批下来后再补上即可。
