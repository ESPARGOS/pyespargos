#version 450

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 vColor;
layout(location = 0) out vec4 fragmentColor;
layout(binding = 1) uniform sampler2D source;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
};

void main(){
    ivec2 size_of_texture = textureSize(source, 0);
    float scale_x = float(size_of_texture.x-1)/float(size_of_texture.x);
    float scale_y = float((size_of_texture.y-1))/float(size_of_texture.y);
    float offset_y = 0.5/float(size_of_texture.y);
    float offset_x = 0.5/float(size_of_texture.x);
    float texture_x = qt_TexCoord0.x*scale_x + offset_x;
    float texture_y = qt_TexCoord0.y*scale_y + offset_y;
    vec4 color = texture(source, vec2(texture_x, texture_y));
    fragmentColor = color;
}