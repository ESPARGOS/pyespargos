#version 450
#define PI 3.1415926535897932384626433832795

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 vColor;
layout(location = 0) out vec4 fragmentColor;
layout(binding = 1) uniform sampler2D source;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
};

vec2 car_to_pol(vec2 car_cor){
    float r = sqrt(car_cor.x*car_cor.x+car_cor.y*car_cor.y);
    float angle = atan(-car_cor.x, car_cor.y);
    vec2 polar = vec2(r, angle);
    return polar;
}

void main(){
    vec2 position = car_to_pol(qt_TexCoord0);
    ivec2 size_of_texture = textureSize(source, 0);
    float scale_x = float(size_of_texture.x-1)/float(size_of_texture.x);
    float scale_y = float((size_of_texture.y-1))/float(size_of_texture.y);
    float offset_y = 0.5/float(size_of_texture.y);
    if(position.x <1){
        float texture_y = position.x*scale_y + offset_y;
        float psi = sin(position.y)*PI;
        float texture_x = psi/(2*PI)*scale_x + 0.5;
        vec4 color = texture(source, vec2(texture_x, texture_y));
        fragmentColor = color;
    }
    else{
        fragmentColor = vec4(0,0,0,0);
    }

}