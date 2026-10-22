output "instance_id" {
  value = aws_instance.box.id
}

output "connect_command" {
  value = "aws ssm start-session --target ${aws_instance.box.id} --region ${var.region}"
}
