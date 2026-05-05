# `infra/iam` — operator IAM policies

## `meridian-operator.json`

Least-privilege IAM policy granting the IAM principal that runs
`terraform apply` exactly the permissions needed to provision and
maintain the two Meridian Terraform modules:

- `infra/terraform/s3/` — the durable raw-sample archive bucket.
- `infra/terraform/ec2-cohabit/` — the EBS volume + Lambda + SSM +
  EventBridge Scheduler + SNS that drive the weekly run on specter's
  EC2 instance.

Resource scoping:

- **S3** — `arn:aws:s3:::meridian-archive-*` (any meridian-prefixed bucket).
- **IAM policies/roles** — `meridian-*` plus `SpecterInstanceRole` (the
  cohabit module attaches inline policies to specter's role; it never
  manages the role itself).
- **`iam:PassRole`** — only meridian-prefixed roles, only to
  `lambda.amazonaws.com` and `scheduler.amazonaws.com`.
- **EC2** — wildcard-resource. The actions are `ec2:*Volume*` plus
  start/stop/describe of instances; AWS does not support resource-level
  conditions on most of these.
- **Lambda / Logs / SNS / Scheduler** — all scoped to `meridian-*`
  resource ARNs.
- **SSM Parameters** — scoped to `parameter/meridian/*` (the namespace
  the cohabit module uses). `ssm:DescribeParameters` is wildcard-only
  per AWS — split into its own statement.

What this policy intentionally does **not** grant:

- KMS key management (we use the AWS-managed `aws/ssm` key for
  SecureStrings; its default policy delegates to the SSM service).
- IAM user management (operator account stays manually managed).
- Anything outside the meridian / specter cohabit name patterns.
- Read access to other accounts.

## Apply

```bash
# 1. Create the policy in your account.
aws iam create-policy \
    --policy-name meridian-operator \
    --policy-document file://infra/iam/meridian-operator.json

# 2. Attach to the operator IAM user (or role).
aws iam attach-user-policy \
    --user-name dg \
    --policy-arn arn:aws:iam::<account-id>:policy/meridian-operator
```

## Updating

When a new Terraform resource type is added to either module:

1. Identify the AWS API actions the resource calls (creation, update,
   read-on-plan, delete).
2. Add them to the appropriate Sid block in `meridian-operator.json`,
   preserving resource scoping where possible.
3. Re-create the policy version:

   ```bash
   aws iam create-policy-version \
       --policy-arn arn:aws:iam::<account-id>:policy/meridian-operator \
       --policy-document file://infra/iam/meridian-operator.json \
       --set-as-default
   ```

   AWS keeps up to 5 versions per policy; older non-default versions
   can be pruned with `aws iam delete-policy-version`.

## Caveats

- Best-effort coverage. AWS doesn't publish an authoritative
  Terraform-resource → IAM-action mapping; this policy was assembled
  by reading the AWS provider source for each resource. Expect to hit
  one or two missing-permission errors during the first apply. Add
  the missing action to the right Sid and re-run; do not regress to
  `AdministratorAccess`.
- Delete/destroy actions are included so `terraform destroy` works
  symmetrically. If you want a truly read-mostly operator (e.g. for
  CI plan-only previews), fork this policy and strip the
  delete/`Put*` actions.
