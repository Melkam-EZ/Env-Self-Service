output "function_name" {
  value = aws_lambda_function.fn.function_name
}

output "function_url" {
  value = aws_lambda_function_url.fn.function_url
}

output "invoke_command" {
  value = "aws lambda invoke --function-name ${aws_lambda_function.fn.function_name} --region ${var.region} /dev/stdout"
}
