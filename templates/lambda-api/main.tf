locals {
  name = "envp-${var.team}-${var.env_name}-${substr(var.env_id, 4, 8)}"
}

data "archive_file" "code" {
  type        = "zip"
  output_path = "${path.module}/function.zip"
  source {
    filename = "index.py"
    content  = <<-PY
      import json, os

      def handler(event, context):
          return {"statusCode": 200,
                  "headers": {"content-type": "application/json"},
                  "body": json.dumps({"message": os.environ["MESSAGE"], "env": os.environ["ENV_ID"]})}
    PY
  }
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  name               = local.name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.fn.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Created first so we control retention; otherwise Lambda makes a log group that never expires.
resource "aws_cloudwatch_log_group" "fn" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 1
}

resource "aws_lambda_function" "fn" {
  function_name    = local.name
  role             = aws_iam_role.fn.arn
  runtime          = "python3.12"
  handler          = "index.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  memory_size      = var.memory_mb
  timeout          = 5

  environment {
    variables = {
      MESSAGE = var.message
      ENV_ID  = var.env_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_url" "fn" {
  function_name      = aws_lambda_function.fn.function_name
  authorization_type = "AWS_IAM" # not public: callers must sign requests
}
