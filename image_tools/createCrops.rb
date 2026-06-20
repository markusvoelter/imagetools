#!/usr/bin/env ruby
require 'mini_magick'

# ===== Configuration =====
# Absolute watermark path passed as 2nd arg. If missing or empty, watermarking
# is disabled for every ratio (even those flagged use_watermark: true).
WATERMARK_PATH  = (ARGV[1].to_s.empty?) ? nil : ARGV[1]
WATERMARK_ALPHA = 0.85            # Transparency: 0.0 (invisible) → 1.0 (opaque)

RATIOS = {
  "16:9" => { ratio: 16.0/9.0, wm_ratio: 0.30, tol: 0.5, process_all: false, orientation: "landscape", use_watermark: true },
  "9:16" => { ratio:  9.0/16.0, wm_ratio: 0.3, tol: 2.0, process_all: false, orientation: "portrait", use_watermark: false },
  #"4:3"  => { ratio:  4.0/3.0,  wm_ratio: 0.25, tol: 0.33, process_all: false, orientation: "landscape", use_watermark: true },
  #"3:4"  => { ratio:  3.0/4.0,  wm_ratio: 0.33, tol: 0.33, process_all: false, orientation: "portrait", use_watermark: true },
  #"1:1"  => { ratio:  1.0,      wm_ratio: 0.20, tol: 0.33, process_all: true,  orientation: "square", use_watermark: true }
}


CROP_VARIANTS = ["01", "05", "10"]

# ===== Calculate offsets and crop size for each variant =====
def calculate_offsets(variant, orientation, width, height, target_ratio)
  case orientation
  when "portrait"
    crop_height = height
    crop_width  = (height * target_ratio).round  # same for all variants
    offset_x = case variant
               when "01" then 0
               when "03" then ((width - crop_width) / 4.0).round
               when "04" then ((width - crop_width) / 3.0).round
               when "05" then ((width - crop_width) / 2.0).round
               when "06" then ((width - crop_width) / 3.0 * 2.0).round
               when "07" then ((width - crop_width) / 4.0 * 3.0).round
               when "10" then (width - crop_width)
               end
    offset_y = 0

  when "landscape"
    if width.to_f / height < target_ratio
      # Image is too tall → crop top/bottom
      crop_width  = width
      crop_height = (width / target_ratio).round
      offset_x = 0
      offset_y = case variant
                when "01" then 0
                when "05" then ((height - crop_height) / 2.0).round
                when "10" then (height - crop_height)
                end
    else
      # Image is too wide → crop left/right
      crop_height = height
      crop_width  = (height * target_ratio).round
      offset_y = 0
      offset_x = case variant
                when "01" then 0
                when "05" then ((width - crop_width) / 2.0).round
                when "10" then (width - crop_width)
                end
    end
  when "square"
    side = [width, height].min
    offset_x = ((width - side)/2.0).round
    offset_y = ((height - side)/2.0).round
    crop_width = crop_height = side
  end

  # Ensure crop dimensions never exceed original image
  #crop_width  = [crop_width, width].min
  #crop_height = [crop_height, height].min

  return offset_x, offset_y, crop_width, crop_height
end

# ===== Process a single cropped variant =====
def process_variant(original_image_path, output_path, crop_width, crop_height, offset_x, offset_y, watermark_ratio, use_watermark)
  img = MiniMagick::Image.open(original_image_path)

  # Crop
  img.crop("#{crop_width}x#{crop_height}+#{offset_x}+#{offset_y}")

  puts "#{original_image_path} cropping to #{crop_width}x#{crop_height}+#{offset_x}+#{offset_y}"

  # Watermark (skipped when no watermark path was supplied)
  if use_watermark && WATERMARK_PATH
    watermark = MiniMagick::Image.open(WATERMARK_PATH)
    wm_target_width = (img.width * watermark_ratio).round
    watermark.resize("#{wm_target_width}x")
    if WATERMARK_ALPHA < 1.0
      watermark.alpha 'set'
      watermark.evaluate 'Multiply', WATERMARK_ALPHA
    end
    img = img.composite(watermark) do |c|
      c.gravity "SouthEast"
      c.geometry "+5+5"
    end
  end

  # Resize
  if img.width >= img.height
    img.resize "1250x"
  else
    img.resize "x1250"
  end

  # Save
  img.write(output_path)
end

# ===== Cropping function for a single image =====
def crop_and_process_image(file, dir, ratio_info)
  input_dir  = File.dirname(file)
  output_dir = File.join(input_dir, "_cropped_#{dir.gsub(":", "_")}")
  Dir.mkdir(output_dir) unless Dir.exist?(output_dir)

  image = MiniMagick::Image.open(file)
  width  = image.width
  height = image.height
  current_ratio = width.to_f / height

  target_ratio     = ratio_info[:ratio]
  watermark_ratio = ratio_info[:wm_ratio]
  tolerance       = ratio_info[:tol]
  process_all     = ratio_info[:process_all]
  orientation     = ratio_info[:orientation]
  use_watermark   = ratio_info[:use_watermark]

  min_ratio = target_ratio * (1 - tolerance)
  max_ratio = target_ratio * (1 + tolerance)

  case orientation
  when "landscape"
    return unless (
      (current_ratio < max_ratio && current_ratio > min_ratio) && current_ratio != target_ratio
    ) || process_all  
  when "portrait"
    return unless true; #current_ratio > target_ratio && current_ratio < max_ratio || process_all  # only crop if image is narrower than target - tolerance
  when "square"
    # always allow square crops if process_all is true
    return unless process_all || current_ratio.between?(min_ratio, max_ratio)
  end

  # Compute how far off the image ratio is
  current_ratio = width.to_f / height

  # Current relative deviation from target ratio
  relative_deviation = (current_ratio - target_ratio).abs / target_ratio

  puts "rel " + relative_deviation.to_s

  # Only generate edge variants if deviation exceeds 20% of the tolerance
  threshold = 0.2 * tolerance  

  # Decide which variants to process
  cond = relative_deviation > threshold && ratio_info[:orientation] != "square"
  variants_to_process = ["05"] # always center
  variants_to_process += ["01", "10"] if cond
  variants_to_process += ["03", "04", "06", "07"] if cond && ratio_info[:orientation] == "portrait"


  # Process selected variants
  variants_to_process.each do |variant|
    offset_x, offset_y, crop_width, crop_height = calculate_offsets(variant, orientation, width, height, target_ratio)

    basename = File.basename(file, File.extname(file))
    ext      = File.extname(file)
    result_file_name = "#{basename}_#{dir.gsub(":", "_")}_#{variant}#{ext}"
    output_path = File.join(output_dir, result_file_name)

    process_variant(file, output_path, crop_width, crop_height, offset_x, offset_y, watermark_ratio, use_watermark)

    puts "  #{dir} [#{variant}] --> #{output_path}"
  end
end

# ===== Batch process for one ratio =====
def process(dir)
  input_dir    = ARGV[0]

  unless input_dir && Dir.exist?(input_dir)
    puts "Input directory missing or does not exist!"
    exit 1
  end

  ratio_info = RATIOS[dir]
  unless ratio_info
    puts "Unsupported ratio '#{dir}'"
    return
  end

  Dir.glob(File.join(input_dir, "*.{jpg,jpeg,png}")).each do |file|
    crop_and_process_image(file, dir, ratio_info)
  end
end

# ===== Main calls =====
RATIOS.keys.each { |dir| process(dir) }