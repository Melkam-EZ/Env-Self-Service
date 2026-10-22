output "bucket" {
  value = aws_s3_bucket.this.id
}

output "copy_command" {
  value = "aws s3 cp ./file.txt s3://${aws_s3_bucket.this.id}/"
}
